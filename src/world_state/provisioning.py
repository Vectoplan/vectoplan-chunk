# services/vectoplan-chunk/src/world_state/provisioning.py
"""
Project provisioning for `vectoplan-chunk`.

Purpose:
    Create or return a chunk-side Project/Universe/WorldInstance structure for
    an external `vectoplan-app` project.

Important service boundary:
    vectoplan-app owns App projects.
    vectoplan-chunk owns Chunk projects.
    The connection is an external app project id stored on the chunk project and
    returned as stable references.

This module is intentionally defensive because model field names may still be in
transition. It uses SQLAlchemy model introspection and only writes fields that
exist on the current model classes.

This module does not:
    - create database tables,
    - run migrations,
    - seed global debug blocks,
    - generate chunks,
    - create ChunkSnapshots,
    - write ChunkEvents,
    - call vectoplan-app.

Expected caller:
    routes/projects.py
        PUT  /projects/by-app/<app_project_public_id>
        POST /projects/ensure

Typical return:
    {
      "ok": true,
      "code": "chunk_project_provisioned",
      "created": true,
      "ids": {
        "externalAppProjectId": "prj_...",
        "chunkProjectId": "chk_prj_...",
        "chunkUniverseId": "chk_uni_...",
        "chunkWorldId": "chk_wld_..."
      },
      "routeHints": {
        "bootstrap": "/projects/chk_prj_.../bootstrap",
        "worlds": "/projects/chk_prj_.../worlds",
        "blocks": "/projects/chk_prj_.../worlds/chk_wld_.../blocks",
        "chunk": "/projects/chk_prj_.../worlds/chk_wld_.../chunks",
        "chunksBatch": "/projects/chk_prj_.../worlds/chk_wld_.../chunks/batch",
        "commands": "/projects/chk_prj_.../worlds/chk_wld_.../commands"
      }
    }
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Mapping, MutableMapping, Sequence

try:
    from flask import current_app, has_app_context
except Exception:  # pragma: no cover - Flask can be unavailable in isolated tests.
    current_app = None  # type: ignore[assignment]

    def has_app_context() -> bool:  # type: ignore[no-redef]
        return False


try:
    from sqlalchemy.exc import IntegrityError, SQLAlchemyError
except Exception:  # pragma: no cover - SQLAlchemy always expected in service runtime.
    IntegrityError = Exception  # type: ignore[misc,assignment]
    SQLAlchemyError = Exception  # type: ignore[misc,assignment]


try:
    from extensions import db
except Exception:  # pragma: no cover - lets import fail gracefully in docs/tests.
    db = None  # type: ignore[assignment]


try:
    from models import BlockRegistry, BlockType, Project, Universe, WorldInstance
except Exception:  # pragma: no cover - lets status tooling still import module.
    Project = None  # type: ignore[assignment]
    Universe = None  # type: ignore[assignment]
    WorldInstance = None  # type: ignore[assignment]
    BlockRegistry = None  # type: ignore[assignment]
    BlockType = None  # type: ignore[assignment]


SAFE_ID_RE = re.compile(r"[^a-zA-Z0-9_\-:.]+")
PROVISIONING_SCHEMA_VERSION = "vectoplan.chunk.project-provisioning.v1"

DEFAULT_PROJECT_PREFIX = "chk_prj_"
DEFAULT_UNIVERSE_PREFIX = "chk_uni_"
DEFAULT_WORLD_PREFIX = "chk_wld_"
DEFAULT_WORLD_ROLE = "default_spawn"
DEFAULT_WORLD_SCOPE = "project"
DEFAULT_TEMPLATE_ID = "flat"
DEFAULT_PROVIDER_WORLD_ID = "flat"
DEFAULT_BLOCK_REGISTRY_ID = "debug-blocks"
DEFAULT_BLOCK_REGISTRY_VERSION = "1"

EXTERNAL_APP_PROJECT_ID_FIELDS = (
    "external_app_project_id",
    "app_project_public_id",
    "app_project_id",
    "source_project_id",
    "origin_project_id",
    "external_project_id",
)

PROJECT_ID_FIELDS = (
    "project_id",
    "public_id",
    "slug",
    "key",
)

UNIVERSE_ID_FIELDS = (
    "universe_id",
    "public_id",
    "slug",
    "key",
)

WORLD_ID_FIELDS = (
    "world_id",
    "public_id",
    "slug",
    "key",
)

NAME_FIELDS = (
    "name",
    "display_name",
    "title",
    "label",
)

DESCRIPTION_FIELDS = (
    "description",
    "summary",
)

STATUS_FIELDS = (
    "status",
    "lifecycle_status",
)

METADATA_FIELDS = (
    "metadata_json",
    "meta_json",
    "settings",
)

CREATED_AT_FIELDS = (
    "created_at",
    "created",
)

UPDATED_AT_FIELDS = (
    "updated_at",
    "updated",
    "modified_at",
)

SOURCE_SERVICE_FIELDS = (
    "source_service",
    "origin_service",
    "created_by_service",
)

PROJECT_RELATION_FIELDS = (
    "project",
)

UNIVERSE_RELATION_FIELDS = (
    "universe",
)

PROJECT_REF_FIELDS = (
    "project_id",
    "project_public_id",
    "project_key",
    "chunk_project_id",
)

UNIVERSE_REF_FIELDS = (
    "universe_id",
    "universe_public_id",
    "universe_key",
    "chunk_universe_id",
)

WORLD_REF_FIELDS = (
    "default_world_id",
    "spawn_world_id",
    "world_id",
)

BLOCK_REGISTRY_ID_FIELDS = (
    "block_registry_id",
    "registry_id",
)

BLOCK_REGISTRY_VERSION_FIELDS = (
    "block_registry_version",
    "registry_version",
)


@dataclass(slots=True)
class ProvisioningIssue:
    """A non-fatal warning or fatal error item."""

    code: str
    message: str
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "details": self.details,
        }


@dataclass(slots=True)
class ChunkProjectProvisioningResult:
    """Structured result for chunk project provisioning."""

    ok: bool
    code: str
    message: str
    created: bool = False
    updated: bool = False
    external_app_project_id: str | None = None
    chunk_project_id: str | None = None
    chunk_universe_id: str | None = None
    chunk_world_id: str | None = None
    block_registry_id: str | None = None
    block_registry_version: str | None = None
    project: dict[str, Any] = field(default_factory=dict)
    universe: dict[str, Any] = field(default_factory=dict)
    world: dict[str, Any] = field(default_factory=dict)
    block_registry: dict[str, Any] = field(default_factory=dict)
    route_hints: dict[str, str] = field(default_factory=dict)
    warnings: list[ProvisioningIssue] = field(default_factory=list)
    errors: list[ProvisioningIssue] = field(default_factory=list)
    status_code: int = 200

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "code": self.code,
            "message": self.message,
            "created": self.created,
            "updated": self.updated,
            "schemaVersion": PROVISIONING_SCHEMA_VERSION,
            "ids": {
                "externalAppProjectId": self.external_app_project_id,
                "chunkProjectId": self.chunk_project_id,
                "chunkUniverseId": self.chunk_universe_id,
                "chunkWorldId": self.chunk_world_id,
                "blockRegistryId": self.block_registry_id,
                "blockRegistryVersion": self.block_registry_version,
            },
            "project": self.project,
            "universe": self.universe,
            "world": self.world,
            "blockRegistry": self.block_registry,
            "routeHints": self.route_hints,
            "warnings": [issue.to_dict() for issue in self.warnings],
            "errors": [issue.to_dict() for issue in self.errors],
        }


class ProvisioningError(RuntimeError):
    """Raised for expected provisioning validation/flow errors."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        details: Mapping[str, Any] | None = None,
        status_code: int = 400,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = dict(details or {})
        self.status_code = status_code


def utcnow() -> datetime:
    """Return timezone-aware UTC datetime."""
    return datetime.now(timezone.utc)


def _safe_str(value: Any, default: str = "", max_len: int | None = None) -> str:
    """Safely normalize any value to a stripped string."""
    if value is None:
        text = default
    else:
        try:
            text = str(value).strip()
        except Exception:
            text = default

    if not text:
        text = default

    if max_len is not None and max_len > 0 and len(text) > max_len:
        text = text[:max_len]

    return text


def _safe_bool(value: Any, default: bool = False) -> bool:
    """Safely normalize booleans from common payload/env representations."""
    if isinstance(value, bool):
        return value

    text = _safe_str(value).lower()
    if not text:
        return default

    if text in {"1", "true", "yes", "y", "on", "enabled"}:
        return True

    if text in {"0", "false", "no", "n", "off", "disabled"}:
        return False

    return default


def _safe_identifier(value: Any, default: str, *, max_len: int = 96) -> str:
    """Normalize an external or generated identifier."""
    raw = _safe_str(value, default=default)
    cleaned = SAFE_ID_RE.sub("-", raw).strip("-._:")

    if not cleaned:
        cleaned = default

    if len(cleaned) > max_len:
        digest = _short_hash(cleaned, length=12)
        cleaned = f"{cleaned[: max_len - 13]}_{digest}"

    return cleaned


def _short_hash(value: Any, *, length: int = 12) -> str:
    """Stable short hash for deterministic ids."""
    text = _safe_str(value, default="unknown")
    digest = hashlib.sha1(text.encode("utf-8", errors="ignore")).hexdigest()
    return digest[: max(6, min(length, 40))]


def _prefixed_id(prefix: str, external_id: str, *, fallback: str) -> str:
    """Build a deterministic chunk-side id from external app project id."""
    safe_external = _safe_identifier(external_id, fallback, max_len=48)
    digest = _short_hash(external_id, length=12)
    return _safe_identifier(f"{prefix}{safe_external}_{digest}", f"{prefix}{digest}", max_len=96)


def _payload_dict(payload: Mapping[str, Any] | None) -> dict[str, Any]:
    """Convert payload-like object to a plain dict."""
    if payload is None:
        return {}

    if isinstance(payload, dict):
        return dict(payload)

    try:
        return dict(payload)
    except Exception:
        return {}


def _payload_get(payload: Mapping[str, Any], names: Sequence[str], default: Any = None) -> Any:
    """Read first non-empty payload value from several possible keys."""
    for name in names:
        if name in payload:
            value = payload.get(name)
            if value is not None and _safe_str(value):
                return value

    return default


def _config_value(name: str, default: Any = None) -> Any:
    """Read config from Flask current_app if available, else fallback."""
    try:
        if has_app_context() and current_app is not None:
            return current_app.config.get(name, default)
    except Exception:
        return default

    return default


def _config_bool(name: str, default: bool = False) -> bool:
    """Read boolean config value."""
    return _safe_bool(_config_value(name, default), default)


def _config_str(name: str, default: str) -> str:
    """Read string config value."""
    return _safe_str(_config_value(name, default), default)


def _config_int(name: str, default: int) -> int:
    """Read int config value."""
    try:
        return int(_config_value(name, default))
    except Exception:
        return default


def _model_available(model: Any) -> bool:
    return model is not None


def _model_columns(model_or_obj: Any) -> set[str]:
    """Return SQLAlchemy column names for a model or model instance."""
    model = model_or_obj if isinstance(model_or_obj, type) else type(model_or_obj)

    try:
        table = getattr(model, "__table__", None)
        columns = getattr(table, "columns", None)
        if columns is not None:
            return {str(column.name) for column in columns}
    except Exception:
        return set()

    return set()


def _model_relationships(model_or_obj: Any) -> set[str]:
    """Return SQLAlchemy relationship names for a model or model instance."""
    model = model_or_obj if isinstance(model_or_obj, type) else type(model_or_obj)

    try:
        mapper = getattr(model, "__mapper__", None)
        relationships = getattr(mapper, "relationships", None)
        if relationships is not None:
            return {str(relationship.key) for relationship in relationships}
    except Exception:
        return set()

    return set()


def _supports_attr(model_or_obj: Any, attr_name: str) -> bool:
    """Check if a model or object supports an attribute/column/relationship."""
    if model_or_obj is None:
        return False

    if attr_name in _model_columns(model_or_obj):
        return True

    if attr_name in _model_relationships(model_or_obj):
        return True

    try:
        return hasattr(model_or_obj, attr_name)
    except Exception:
        return False


def _column_python_type(model_or_obj: Any, attr_name: str) -> type[Any] | None:
    """Return SQLAlchemy column python_type if available."""
    model = model_or_obj if isinstance(model_or_obj, type) else type(model_or_obj)

    try:
        table = getattr(model, "__table__", None)
        columns = getattr(table, "columns", None)
        column = columns.get(attr_name) if columns is not None else None
        if column is None:
            return None
        return column.type.python_type
    except Exception:
        return None


def _set_if_supported(
    obj: Any,
    fields: Sequence[str],
    value: Any,
    *,
    allow_empty: bool = False,
    overwrite: bool = True,
) -> str | None:
    """
    Set the first supported field on an object.

    Returns the field that was set, or None.
    """
    if obj is None:
        return None

    if not allow_empty and value in (None, ""):
        return None

    for field_name in fields:
        if not _supports_attr(obj, field_name):
            continue

        try:
            current_value = getattr(obj, field_name, None)
        except Exception:
            current_value = None

        if not overwrite and current_value not in (None, ""):
            return field_name

        try:
            setattr(obj, field_name, value)
            return field_name
        except Exception:
            continue

    return None


def _set_all_supported(
    obj: Any,
    fields: Sequence[str],
    value: Any,
    *,
    allow_empty: bool = False,
    overwrite: bool = True,
) -> list[str]:
    """Set all supported fields on an object."""
    written: list[str] = []

    for field_name in fields:
        if _set_if_supported(
            obj,
            (field_name,),
            value,
            allow_empty=allow_empty,
            overwrite=overwrite,
        ):
            written.append(field_name)

    return written


def _set_timestamp_fields(obj: Any, *, created: bool, updated: bool) -> None:
    """Best-effort timestamp field assignment."""
    now = utcnow()

    if created:
        for field_name in CREATED_AT_FIELDS:
            _set_if_supported(obj, (field_name,), now, overwrite=False)

    if updated:
        for field_name in UPDATED_AT_FIELDS:
            _set_if_supported(obj, (field_name,), now, overwrite=True)


def _read_json_field(obj: Any, fields: Sequence[str] = METADATA_FIELDS) -> dict[str, Any]:
    """Read first metadata/settings JSON field as dict."""
    if obj is None:
        return {}

    for field_name in fields:
        if not _supports_attr(obj, field_name):
            continue

        try:
            value = getattr(obj, field_name)
        except Exception:
            continue

        if isinstance(value, dict):
            return dict(value)

        if isinstance(value, str) and value.strip():
            try:
                parsed = json.loads(value)
            except Exception:
                parsed = None

            if isinstance(parsed, dict):
                return parsed

    return {}


def _write_json_field(obj: Any, value: Mapping[str, Any], fields: Sequence[str] = METADATA_FIELDS) -> str | None:
    """Write metadata/settings JSON to first supported field."""
    if obj is None:
        return None

    data = dict(value)

    for field_name in fields:
        if not _supports_attr(obj, field_name):
            continue

        try:
            current_value = getattr(obj, field_name, None)
        except Exception:
            current_value = None

        try:
            if isinstance(current_value, str):
                setattr(obj, field_name, json.dumps(data, ensure_ascii=False, sort_keys=True))
            else:
                setattr(obj, field_name, data)
            return field_name
        except Exception:
            continue

    return None


def _merge_json_field(obj: Any, update: Mapping[str, Any]) -> dict[str, Any]:
    """Merge provisioning metadata into existing metadata/settings field."""
    current = _read_json_field(obj)
    current.update(dict(update))
    _write_json_field(obj, current)
    return current


def _assign_relationship_or_ref(
    obj: Any,
    relation_fields: Sequence[str],
    ref_fields: Sequence[str],
    target_obj: Any,
    target_public_id: str,
) -> None:
    """
    Assign relationship if present; otherwise assign a FK/reference field.

    Introspection is used to decide whether FK fields are integer-like or string-like.
    """
    if obj is None or target_obj is None:
        return

    for relation_name in relation_fields:
        if _supports_attr(obj, relation_name):
            try:
                setattr(obj, relation_name, target_obj)
                return
            except Exception:
                pass

    target_numeric_id = getattr(target_obj, "id", None)

    for field_name in ref_fields:
        if not _supports_attr(obj, field_name):
            continue

        python_type = _column_python_type(obj, field_name)

        try:
            if python_type is int and target_numeric_id is not None:
                setattr(obj, field_name, target_numeric_id)
                return

            if python_type is str:
                setattr(obj, field_name, target_public_id)
                return

            if target_numeric_id is not None and field_name.endswith("_id"):
                setattr(obj, field_name, target_numeric_id)
                return

            setattr(obj, field_name, target_public_id)
            return
        except Exception:
            continue


def _query_one_by_field(session: Any, model: Any, field_name: str, value: Any) -> Any | None:
    """Query one record by a supported model field."""
    if session is None or model is None or value in (None, ""):
        return None

    if not _supports_attr(model, field_name):
        return None

    try:
        column = getattr(model, field_name)
        query = session.query(model).filter(column == value)
        return query.one_or_none()
    except Exception:
        try:
            return session.query(model).filter_by(**{field_name: value}).first()
        except Exception:
            return None


def _query_first_by_fields(
    session: Any,
    model: Any,
    candidates: Mapping[str, Any],
) -> Any | None:
    """Try several field/value lookups and return the first result."""
    for field_name, value in candidates.items():
        found = _query_one_by_field(session, model, field_name, value)
        if found is not None:
            return found

    return None


def _serialize_model(obj: Any, fields: Sequence[str]) -> dict[str, Any]:
    """Serialize selected scalar fields from a model instance."""
    if obj is None:
        return {}

    result: dict[str, Any] = {}

    for field_name in fields:
        if not _supports_attr(obj, field_name):
            continue

        try:
            value = getattr(obj, field_name)
        except Exception:
            continue

        if isinstance(value, datetime):
            result[field_name] = value.isoformat()
        elif isinstance(value, (str, int, float, bool)) or value is None:
            result[field_name] = value
        elif isinstance(value, dict):
            result[field_name] = value
        else:
            result[field_name] = _safe_str(value)

    return result


def _extract_id(obj: Any, fields: Sequence[str], fallback: str | None = None) -> str | None:
    """Extract first supported non-empty id-like field."""
    if obj is None:
        return fallback

    for field_name in fields:
        if not _supports_attr(obj, field_name):
            continue

        try:
            value = getattr(obj, field_name)
        except Exception:
            continue

        text = _safe_str(value)
        if text:
            return text

    return fallback


def _build_route_hints(chunk_project_id: str, chunk_world_id: str) -> dict[str, str]:
    """Build project-scoped chunk route hints."""
    base = f"/projects/{chunk_project_id}"
    world_base = f"{base}/worlds/{chunk_world_id}"

    return {
        "project": base,
        "bootstrap": f"{base}/bootstrap",
        "worlds": f"{base}/worlds",
        "world": world_base,
        "blocks": f"{world_base}/blocks",
        "chunk": f"{world_base}/chunks",
        "chunks": f"{world_base}/chunks",
        "chunksBatch": f"{world_base}/chunks/batch",
        "commands": f"{world_base}/commands",
    }


def _default_ids_for_external_project(
    external_app_project_id: str,
    payload: Mapping[str, Any],
) -> tuple[str, str, str]:
    """Resolve deterministic chunk project/universe/world ids."""
    project_prefix = _config_str(
        "VECTOPLAN_CHUNK_PROJECT_PROVISIONING_PROJECT_ID_PREFIX",
        DEFAULT_PROJECT_PREFIX,
    )
    universe_prefix = _config_str(
        "VECTOPLAN_CHUNK_PROJECT_PROVISIONING_UNIVERSE_ID_PREFIX",
        DEFAULT_UNIVERSE_PREFIX,
    )
    world_prefix = _config_str(
        "VECTOPLAN_CHUNK_PROJECT_PROVISIONING_WORLD_ID_PREFIX",
        DEFAULT_WORLD_PREFIX,
    )

    explicit_chunk_project_id = _payload_get(
        payload,
        (
            "chunk_project_id",
            "chunkProjectId",
            "project_id",
            "projectId",
        ),
        None,
    )

    explicit_universe_id = _payload_get(
        payload,
        (
            "chunk_universe_id",
            "chunkUniverseId",
            "universe_id",
            "universeId",
        ),
        None,
    )

    explicit_world_id = _payload_get(
        payload,
        (
            "chunk_world_id",
            "chunkWorldId",
            "world_id",
            "worldId",
        ),
        None,
    )

    chunk_project_id = (
        _safe_identifier(explicit_chunk_project_id, "chunk-project")
        if explicit_chunk_project_id
        else _prefixed_id(project_prefix, external_app_project_id, fallback="app-project")
    )

    chunk_universe_id = (
        _safe_identifier(explicit_universe_id, "chunk-universe")
        if explicit_universe_id
        else _prefixed_id(universe_prefix, external_app_project_id, fallback="app-project")
    )

    world_id_mode = _safe_str(
        _config_value("VECTOPLAN_CHUNK_PROJECT_PROVISIONING_WORLD_ID_MODE", "global-unique"),
        "global-unique",
    ).lower()

    configured_default_world_id = _config_str(
        "VECTOPLAN_CHUNK_PROJECT_PROVISIONING_DEFAULT_WORLD_ID",
        "world_spawn",
    )

    if explicit_world_id:
        chunk_world_id = _safe_identifier(explicit_world_id, "chunk-world")
    elif world_id_mode in {"shared", "project-scoped", "project_scoped", "world_spawn"}:
        chunk_world_id = _safe_identifier(configured_default_world_id, "world_spawn")
    else:
        chunk_world_id = _prefixed_id(world_prefix, external_app_project_id, fallback="app-project")

    return chunk_project_id, chunk_universe_id, chunk_world_id


def _build_project_metadata(
    external_app_project_id: str,
    payload: Mapping[str, Any],
    chunk_project_id: str,
    chunk_universe_id: str,
    chunk_world_id: str,
) -> dict[str, Any]:
    """Build metadata stored on the chunk project."""
    route_hints = _build_route_hints(chunk_project_id, chunk_world_id)

    payload_metadata = _payload_get(
        payload,
        (
            "metadata",
            "metadata_json",
            "metadataJson",
            "project_metadata",
            "projectMetadata",
        ),
        {},
    )

    if not isinstance(payload_metadata, Mapping):
        payload_metadata = {}

    return {
        "schemaVersion": PROVISIONING_SCHEMA_VERSION,
        "sourceService": _config_str(
            "VECTOPLAN_CHUNK_PROJECT_PROVISIONING_SOURCE_SERVICE",
            "vectoplan-app",
        ),
        "externalAppProjectId": external_app_project_id,
        "chunkProjectId": chunk_project_id,
        "chunkUniverseId": chunk_universe_id,
        "chunkWorldId": chunk_world_id,
        "templateId": _config_str(
            "VECTOPLAN_CHUNK_PROJECT_PROVISIONING_DEFAULT_TEMPLATE_ID",
            DEFAULT_TEMPLATE_ID,
        ),
        "providerWorldId": _config_str(
            "VECTOPLAN_CHUNK_DEFAULT_PROVIDER_WORLD_ID",
            DEFAULT_PROVIDER_WORLD_ID,
        ),
        "routeHints": route_hints,
        "appPayload": dict(payload_metadata),
        "provisionedAt": utcnow().isoformat(),
    }


def _project_name_from_payload(payload: Mapping[str, Any], external_app_project_id: str) -> str:
    """Resolve project display name from payload."""
    return _safe_str(
        _payload_get(
            payload,
            (
                "name",
                "project_name",
                "projectName",
                "title",
                "display_name",
                "displayName",
            ),
            f"Chunk Project for {external_app_project_id}",
        ),
        f"Chunk Project for {external_app_project_id}",
        max_len=240,
    )


def _project_description_from_payload(payload: Mapping[str, Any]) -> str:
    """Resolve project description from payload."""
    return _safe_str(
        _payload_get(
            payload,
            (
                "description",
                "summary",
                "project_description",
                "projectDescription",
            ),
            "",
        ),
        "",
        max_len=2000,
    )


def _ensure_session(session: Any | None) -> Any:
    """Return provided session or global db.session."""
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
            details={"error": repr(exc)},
            status_code=500,
        ) from exc


def _ensure_models_available() -> None:
    """Validate required model classes are importable."""
    missing: list[str] = []

    if not _model_available(Project):
        missing.append("Project")
    if not _model_available(Universe):
        missing.append("Universe")
    if not _model_available(WorldInstance):
        missing.append("WorldInstance")

    if missing:
        raise ProvisioningError(
            "models_unavailable",
            "Required provisioning models are not available.",
            details={"missing": missing},
            status_code=500,
        )


def _find_existing_project(
    session: Any,
    external_app_project_id: str,
    chunk_project_id: str,
) -> Any | None:
    """Find existing chunk project by external app id or deterministic project id."""
    candidates: dict[str, Any] = {}

    for field_name in EXTERNAL_APP_PROJECT_ID_FIELDS:
        candidates[field_name] = external_app_project_id

    for field_name in PROJECT_ID_FIELDS:
        candidates[field_name] = chunk_project_id

    return _query_first_by_fields(session, Project, candidates)


def _find_existing_universe(
    session: Any,
    chunk_universe_id: str,
) -> Any | None:
    """Find existing universe by deterministic universe id."""
    candidates = {field_name: chunk_universe_id for field_name in UNIVERSE_ID_FIELDS}
    return _query_first_by_fields(session, Universe, candidates)


def _find_existing_world(
    session: Any,
    chunk_world_id: str,
) -> Any | None:
    """Find existing world by deterministic world id."""
    candidates = {field_name: chunk_world_id for field_name in WORLD_ID_FIELDS}
    return _query_first_by_fields(session, WorldInstance, candidates)


def _find_default_block_registry(session: Any) -> Any | None:
    """Find the configured default block registry, if the model exists."""
    if not _model_available(BlockRegistry):
        return None

    registry_id = _config_str(
        "VECTOPLAN_CHUNK_DEFAULT_BLOCK_REGISTRY_ID",
        DEFAULT_BLOCK_REGISTRY_ID,
    )
    registry_version = _config_str(
        "VECTOPLAN_CHUNK_DEFAULT_BLOCK_REGISTRY_VERSION",
        DEFAULT_BLOCK_REGISTRY_VERSION,
    )

    registry_id_field = next(
        (field for field in BLOCK_REGISTRY_ID_FIELDS if _supports_attr(BlockRegistry, field)),
        None,
    )

    registry_version_field = next(
        (field for field in BLOCK_REGISTRY_VERSION_FIELDS if _supports_attr(BlockRegistry, field)),
        None,
    )

    try:
        query = session.query(BlockRegistry)

        if registry_id_field is not None:
            query = query.filter(getattr(BlockRegistry, registry_id_field) == registry_id)

        if registry_version_field is not None:
            query = query.filter(getattr(BlockRegistry, registry_version_field) == registry_version)

        return query.first()
    except Exception:
        return None


def _new_model_instance(model: Any, model_name: str) -> Any:
    """Create SQLAlchemy model instance with robust error reporting."""
    try:
        return model()
    except Exception as exc:
        raise ProvisioningError(
            "model_instance_create_failed",
            f"Could not instantiate {model_name}.",
            details={"model": model_name, "error": repr(exc)},
            status_code=500,
        ) from exc


def _apply_project_fields(
    project: Any,
    *,
    external_app_project_id: str,
    chunk_project_id: str,
    chunk_universe_id: str,
    chunk_world_id: str,
    payload: Mapping[str, Any],
    created: bool,
) -> None:
    """Apply chunk project fields."""
    name = _project_name_from_payload(payload, external_app_project_id)
    description = _project_description_from_payload(payload)
    source_service = _config_str(
        "VECTOPLAN_CHUNK_PROJECT_PROVISIONING_SOURCE_SERVICE",
        "vectoplan-app",
    )

    _set_if_supported(project, PROJECT_ID_FIELDS, chunk_project_id, overwrite=created)
    _set_all_supported(project, EXTERNAL_APP_PROJECT_ID_FIELDS, external_app_project_id, overwrite=True)
    _set_if_supported(project, NAME_FIELDS, name, overwrite=True)
    _set_if_supported(project, DESCRIPTION_FIELDS, description, allow_empty=True, overwrite=True)
    _set_if_supported(project, STATUS_FIELDS, "active", overwrite=False)
    _set_if_supported(project, SOURCE_SERVICE_FIELDS, source_service, overwrite=True)

    _set_if_supported(project, ("default_universe_id", "primary_universe_id"), chunk_universe_id, overwrite=True)
    _set_if_supported(project, ("default_world_id", "spawn_world_id", "primary_world_id"), chunk_world_id, overwrite=True)

    metadata = _build_project_metadata(
        external_app_project_id,
        payload,
        chunk_project_id,
        chunk_universe_id,
        chunk_world_id,
    )
    _merge_json_field(project, metadata)
    _set_timestamp_fields(project, created=created, updated=True)


def _apply_universe_fields(
    universe: Any,
    *,
    project: Any,
    chunk_project_id: str,
    chunk_universe_id: str,
    chunk_world_id: str,
    payload: Mapping[str, Any],
    created: bool,
) -> None:
    """Apply universe fields."""
    universe_name = _safe_str(
        _payload_get(
            payload,
            (
                "universe_name",
                "universeName",
            ),
            _config_str(
                "VECTOPLAN_CHUNK_PROJECT_PROVISIONING_DEFAULT_UNIVERSE_NAME",
                "Project Universe",
            ),
        ),
        "Project Universe",
        max_len=240,
    )

    _set_if_supported(universe, UNIVERSE_ID_FIELDS, chunk_universe_id, overwrite=created)
    _set_if_supported(universe, NAME_FIELDS, universe_name, overwrite=True)
    _set_if_supported(universe, STATUS_FIELDS, "active", overwrite=False)
    _assign_relationship_or_ref(
        universe,
        PROJECT_RELATION_FIELDS,
        PROJECT_REF_FIELDS,
        project,
        chunk_project_id,
    )

    _set_if_supported(universe, ("default_world_id", "spawn_world_id", "primary_world_id"), chunk_world_id, overwrite=True)

    _merge_json_field(
        universe,
        {
            "schemaVersion": PROVISIONING_SCHEMA_VERSION,
            "chunkProjectId": chunk_project_id,
            "chunkUniverseId": chunk_universe_id,
            "chunkWorldId": chunk_world_id,
            "provisionedAt": utcnow().isoformat(),
        },
    )
    _set_timestamp_fields(universe, created=created, updated=True)


def _apply_world_fields(
    world: Any,
    *,
    project: Any,
    universe: Any,
    chunk_project_id: str,
    chunk_universe_id: str,
    chunk_world_id: str,
    payload: Mapping[str, Any],
    created: bool,
) -> None:
    """Apply concrete editable world fields."""
    world_name = _safe_str(
        _payload_get(
            payload,
            (
                "world_name",
                "worldName",
            ),
            _config_str(
                "VECTOPLAN_CHUNK_PROJECT_PROVISIONING_DEFAULT_WORLD_NAME",
                "Spawn World",
            ),
        ),
        "Spawn World",
        max_len=240,
    )

    template_id = _safe_identifier(
        _payload_get(
            payload,
            (
                "template_id",
                "templateId",
                "world_template_id",
                "worldTemplateId",
            ),
            _config_str(
                "VECTOPLAN_CHUNK_PROJECT_PROVISIONING_DEFAULT_TEMPLATE_ID",
                DEFAULT_TEMPLATE_ID,
            ),
        ),
        DEFAULT_TEMPLATE_ID,
    )

    provider_world_id = _safe_identifier(
        _payload_get(
            payload,
            (
                "provider_world_id",
                "providerWorldId",
            ),
            _config_str("VECTOPLAN_CHUNK_DEFAULT_PROVIDER_WORLD_ID", DEFAULT_PROVIDER_WORLD_ID),
        ),
        DEFAULT_PROVIDER_WORLD_ID,
    )

    block_registry_id = _config_str(
        "VECTOPLAN_CHUNK_DEFAULT_BLOCK_REGISTRY_ID",
        DEFAULT_BLOCK_REGISTRY_ID,
    )

    block_registry_version = _config_str(
        "VECTOPLAN_CHUNK_DEFAULT_BLOCK_REGISTRY_VERSION",
        DEFAULT_BLOCK_REGISTRY_VERSION,
    )

    _set_if_supported(world, WORLD_ID_FIELDS, chunk_world_id, overwrite=created)
    _set_if_supported(world, NAME_FIELDS, world_name, overwrite=True)
    _set_if_supported(world, STATUS_FIELDS, "active", overwrite=False)

    _assign_relationship_or_ref(
        world,
        UNIVERSE_RELATION_FIELDS,
        UNIVERSE_REF_FIELDS,
        universe,
        chunk_universe_id,
    )

    # Some model versions may store a project reference directly on the world.
    _assign_relationship_or_ref(
        world,
        PROJECT_RELATION_FIELDS,
        PROJECT_REF_FIELDS,
        project,
        chunk_project_id,
    )

    _set_if_supported(world, ("template_id", "world_template_id"), template_id, overwrite=True)
    _set_if_supported(world, ("provider_world_id", "provider_id"), provider_world_id, overwrite=True)

    _set_if_supported(world, ("world_type", "type"), "runtime-world", overwrite=False)
    _set_if_supported(world, ("world_role", "role"), DEFAULT_WORLD_ROLE, overwrite=False)
    _set_if_supported(world, ("world_scope", "scope"), DEFAULT_WORLD_SCOPE, overwrite=False)

    _set_if_supported(world, ("generator_type",), _config_str("VECTOPLAN_CHUNK_DEFAULT_GENERATOR_TYPE", "flat-world"), overwrite=True)
    _set_if_supported(world, ("generator_version",), _config_str("VECTOPLAN_CHUNK_DEFAULT_GENERATOR_VERSION", "1"), overwrite=True)
    _set_if_supported(world, ("projection_type",), _config_str("VECTOPLAN_CHUNK_DEFAULT_PROJECTION_TYPE", "flat-local-v1"), overwrite=True)
    _set_if_supported(world, ("topology_type",), _config_str("VECTOPLAN_CHUNK_DEFAULT_TOPOLOGY_TYPE", "flat-unbounded-v1"), overwrite=True)
    _set_if_supported(world, ("coordinate_system",), _config_str("VECTOPLAN_CHUNK_DEFAULT_COORDINATE_SYSTEM", "vectoplan-world-y-up-v1"), overwrite=True)

    _set_if_supported(world, ("chunk_size",), _config_int("VECTOPLAN_CHUNK_DEFAULT_CHUNK_SIZE", 16), overwrite=True)
    _set_if_supported(world, ("cell_size",), _config_value("VECTOPLAN_CHUNK_DEFAULT_CELL_SIZE", 1.0), overwrite=True)
    _set_if_supported(world, ("surface_y",), _config_int("VECTOPLAN_CHUNK_DEFAULT_SURFACE_Y", 0), overwrite=True)
    _set_if_supported(world, ("min_y",), _config_int("VECTOPLAN_CHUNK_DEFAULT_MIN_Y", -8), overwrite=True)
    _set_if_supported(world, ("max_y",), _config_int("VECTOPLAN_CHUNK_DEFAULT_MAX_Y", 64), overwrite=True)
    _set_if_supported(world, ("seed",), _config_str("VECTOPLAN_CHUNK_DEFAULT_SEED", "dev-seed"), overwrite=True)

    _set_if_supported(world, BLOCK_REGISTRY_ID_FIELDS, block_registry_id, overwrite=True)
    _set_if_supported(world, BLOCK_REGISTRY_VERSION_FIELDS, block_registry_version, overwrite=True)

    _merge_json_field(
        world,
        {
            "schemaVersion": PROVISIONING_SCHEMA_VERSION,
            "chunkProjectId": chunk_project_id,
            "chunkUniverseId": chunk_universe_id,
            "chunkWorldId": chunk_world_id,
            "templateId": template_id,
            "providerWorldId": provider_world_id,
            "blockRegistryId": block_registry_id,
            "blockRegistryVersion": block_registry_version,
            "spawn": {
                "position": {
                    "x": _config_int("VECTOPLAN_CHUNK_DEFAULT_SPAWN_X", 0),
                    "y": _config_int("VECTOPLAN_CHUNK_DEFAULT_SPAWN_Y", 2),
                    "z": _config_int("VECTOPLAN_CHUNK_DEFAULT_SPAWN_Z", 0),
                },
                "rotation": {
                    "yaw": _config_value("VECTOPLAN_CHUNK_DEFAULT_SPAWN_YAW", 0.0),
                    "pitch": _config_value("VECTOPLAN_CHUNK_DEFAULT_SPAWN_PITCH", 0.0),
                },
            },
            "provisionedAt": utcnow().isoformat(),
        },
    )
    _set_timestamp_fields(world, created=created, updated=True)


def _build_success_result(
    *,
    created: bool,
    updated: bool,
    external_app_project_id: str,
    chunk_project_id: str,
    chunk_universe_id: str,
    chunk_world_id: str,
    project: Any,
    universe: Any,
    world: Any,
    block_registry: Any | None,
    warnings: list[ProvisioningIssue],
) -> ChunkProjectProvisioningResult:
    """Build a successful provisioning result."""
    block_registry_id = _config_str(
        "VECTOPLAN_CHUNK_DEFAULT_BLOCK_REGISTRY_ID",
        DEFAULT_BLOCK_REGISTRY_ID,
    )
    block_registry_version = _config_str(
        "VECTOPLAN_CHUNK_DEFAULT_BLOCK_REGISTRY_VERSION",
        DEFAULT_BLOCK_REGISTRY_VERSION,
    )

    route_hints = _build_route_hints(chunk_project_id, chunk_world_id)

    project_payload = _serialize_model(
        project,
        (
            "id",
            "project_id",
            "public_id",
            "slug",
            "name",
            "display_name",
            "title",
            "description",
            "status",
            "external_app_project_id",
            "app_project_public_id",
            "app_project_id",
            "default_universe_id",
            "default_world_id",
            "spawn_world_id",
            "created_at",
            "updated_at",
            "metadata_json",
        ),
    )

    universe_payload = _serialize_model(
        universe,
        (
            "id",
            "universe_id",
            "public_id",
            "slug",
            "name",
            "display_name",
            "title",
            "status",
            "project_id",
            "default_world_id",
            "spawn_world_id",
            "created_at",
            "updated_at",
            "metadata_json",
        ),
    )

    world_payload = _serialize_model(
        world,
        (
            "id",
            "world_id",
            "public_id",
            "slug",
            "name",
            "display_name",
            "title",
            "status",
            "project_id",
            "universe_id",
            "template_id",
            "world_template_id",
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
            "block_registry_id",
            "block_registry_version",
            "created_at",
            "updated_at",
            "metadata_json",
        ),
    )

    block_registry_payload = _serialize_model(
        block_registry,
        (
            "id",
            "registry_id",
            "block_registry_id",
            "registry_version",
            "block_registry_version",
            "name",
            "label",
            "status",
        ),
    )

    code = "chunk_project_provisioned" if created else "chunk_project_exists"
    message = (
        "Chunk project was provisioned."
        if created
        else "Chunk project already exists and was returned."
    )

    return ChunkProjectProvisioningResult(
        ok=True,
        code=code,
        message=message,
        created=created,
        updated=updated,
        external_app_project_id=external_app_project_id,
        chunk_project_id=chunk_project_id,
        chunk_universe_id=chunk_universe_id,
        chunk_world_id=chunk_world_id,
        block_registry_id=block_registry_id,
        block_registry_version=block_registry_version,
        project=project_payload,
        universe=universe_payload,
        world=world_payload,
        block_registry=block_registry_payload,
        route_hints=route_hints,
        warnings=warnings,
        errors=[],
        status_code=201 if created else 200,
    )


def _build_error_result(
    *,
    code: str,
    message: str,
    status_code: int = 400,
    details: Mapping[str, Any] | None = None,
) -> ChunkProjectProvisioningResult:
    """Build an error provisioning result."""
    return ChunkProjectProvisioningResult(
        ok=False,
        code=code,
        message=message,
        errors=[
            ProvisioningIssue(
                code=code,
                message=message,
                details=dict(details or {}),
            )
        ],
        status_code=status_code,
    )


def preview_chunk_project_ids(
    app_project_public_id: str,
    payload: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """
    Preview deterministic chunk ids without touching the database.
    """
    data = _payload_dict(payload)
    external_app_project_id = _safe_identifier(
        app_project_public_id,
        "app-project",
        max_len=128,
    )

    chunk_project_id, chunk_universe_id, chunk_world_id = _default_ids_for_external_project(
        external_app_project_id,
        data,
    )

    return {
        "externalAppProjectId": external_app_project_id,
        "chunkProjectId": chunk_project_id,
        "chunkUniverseId": chunk_universe_id,
        "chunkWorldId": chunk_world_id,
        "routeHints": _build_route_hints(chunk_project_id, chunk_world_id),
    }


def ensure_chunk_project_for_app_project(
    app_project_public_id: str,
    payload: Mapping[str, Any] | None = None,
    *,
    session: Any | None = None,
    commit: bool = True,
) -> ChunkProjectProvisioningResult:
    """
    Idempotently ensure a chunk project for an external app project.

    Parameters:
        app_project_public_id:
            Public id from vectoplan-app, e.g. prj_....

        payload:
            Optional app project metadata. Accepted keys are intentionally broad:
            name/projectName/title, description, metadata, chunk ids, world/template ids.

        session:
            Optional SQLAlchemy session. Defaults to db.session.

        commit:
            If true, this function commits. If false, caller owns transaction.

    Returns:
        ChunkProjectProvisioningResult
    """
    data = _payload_dict(payload)
    warnings: list[ProvisioningIssue] = []

    try:
        if not _config_bool("VECTOPLAN_CHUNK_PROJECT_PROVISIONING_ENABLED", True):
            return _build_error_result(
                code="project_provisioning_disabled",
                message="Chunk project provisioning is disabled by configuration.",
                status_code=503,
            )

        _ensure_models_available()
        active_session = _ensure_session(session)

        external_app_project_id = _safe_identifier(
            app_project_public_id
            or _payload_get(
                data,
                (
                    "app_project_public_id",
                    "appProjectPublicId",
                    "app_project_id",
                    "appProjectId",
                    "external_app_project_id",
                    "externalAppProjectId",
                ),
                "",
            ),
            "app-project",
            max_len=128,
        )

        if not external_app_project_id:
            raise ProvisioningError(
                "missing_app_project_public_id",
                "External app project id is required.",
                status_code=400,
            )

        chunk_project_id, chunk_universe_id, chunk_world_id = _default_ids_for_external_project(
            external_app_project_id,
            data,
        )

        existing_project = _find_existing_project(
            active_session,
            external_app_project_id,
            chunk_project_id,
        )

        created_project = existing_project is None

        if existing_project is None:
            project = _new_model_instance(Project, "Project")
            _apply_project_fields(
                project,
                external_app_project_id=external_app_project_id,
                chunk_project_id=chunk_project_id,
                chunk_universe_id=chunk_universe_id,
                chunk_world_id=chunk_world_id,
                payload=data,
                created=True,
            )
            active_session.add(project)
            active_session.flush()
        else:
            project = existing_project
            allow_name_update = _config_bool(
                "VECTOPLAN_CHUNK_PROJECT_PROVISIONING_ALLOW_NAME_UPDATE",
                True,
            )
            allow_metadata_update = _config_bool(
                "VECTOPLAN_CHUNK_PROJECT_PROVISIONING_ALLOW_METADATA_UPDATE",
                True,
            )

            if allow_name_update or allow_metadata_update:
                _apply_project_fields(
                    project,
                    external_app_project_id=external_app_project_id,
                    chunk_project_id=chunk_project_id,
                    chunk_universe_id=chunk_universe_id,
                    chunk_world_id=chunk_world_id,
                    payload=data,
                    created=False,
                )
                active_session.add(project)
                active_session.flush()

        # Preserve actual ids from existing DB rows if fields use another naming scheme.
        chunk_project_id = _extract_id(project, PROJECT_ID_FIELDS, chunk_project_id) or chunk_project_id

        create_universe = _config_bool(
            "VECTOPLAN_CHUNK_PROJECT_PROVISIONING_CREATE_UNIVERSE",
            True,
        )

        if not create_universe:
            raise ProvisioningError(
                "universe_creation_disabled",
                "Chunk project provisioning requires Universe creation, but it is disabled.",
                status_code=500,
            )

        existing_universe = _find_existing_universe(active_session, chunk_universe_id)
        created_universe = existing_universe is None

        if existing_universe is None:
            universe = _new_model_instance(Universe, "Universe")
        else:
            universe = existing_universe

        _apply_universe_fields(
            universe,
            project=project,
            chunk_project_id=chunk_project_id,
            chunk_universe_id=chunk_universe_id,
            chunk_world_id=chunk_world_id,
            payload=data,
            created=created_universe,
        )

        active_session.add(universe)
        active_session.flush()

        chunk_universe_id = _extract_id(universe, UNIVERSE_ID_FIELDS, chunk_universe_id) or chunk_universe_id

        create_world = _config_bool(
            "VECTOPLAN_CHUNK_PROJECT_PROVISIONING_CREATE_WORLD",
            True,
        )

        if not create_world:
            raise ProvisioningError(
                "world_creation_disabled",
                "Chunk project provisioning requires WorldInstance creation, but it is disabled.",
                status_code=500,
            )

        existing_world = _find_existing_world(active_session, chunk_world_id)
        created_world = existing_world is None

        if existing_world is None:
            world = _new_model_instance(WorldInstance, "WorldInstance")
        else:
            world = existing_world

        _apply_world_fields(
            world,
            project=project,
            universe=universe,
            chunk_project_id=chunk_project_id,
            chunk_universe_id=chunk_universe_id,
            chunk_world_id=chunk_world_id,
            payload=data,
            created=created_world,
        )

        active_session.add(world)
        active_session.flush()

        chunk_world_id = _extract_id(world, WORLD_ID_FIELDS, chunk_world_id) or chunk_world_id

        # Backfill refs after actual ids are known.
        _set_if_supported(project, ("default_universe_id", "primary_universe_id"), chunk_universe_id, overwrite=True)
        _set_if_supported(project, ("default_world_id", "spawn_world_id", "primary_world_id"), chunk_world_id, overwrite=True)
        _set_if_supported(universe, ("default_world_id", "spawn_world_id", "primary_world_id"), chunk_world_id, overwrite=True)

        block_registry = _find_default_block_registry(active_session)
        if block_registry is None:
            warnings.append(
                ProvisioningIssue(
                    code="default_block_registry_not_found",
                    message=(
                        "Default block registry was not found. "
                        "DB bootstrap should seed debug-blocks before runtime usage."
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

        active_session.flush()

        if commit:
            active_session.commit()

        created_any = created_project or created_universe or created_world
        updated = not created_any

        return _build_success_result(
            created=created_any,
            updated=updated,
            external_app_project_id=external_app_project_id,
            chunk_project_id=chunk_project_id,
            chunk_universe_id=chunk_universe_id,
            chunk_world_id=chunk_world_id,
            project=project,
            universe=universe,
            world=world,
            block_registry=block_registry,
            warnings=warnings,
        )

    except ProvisioningError as exc:
        if commit:
            try:
                _ensure_session(session).rollback()
            except Exception:
                pass

        return _build_error_result(
            code=exc.code,
            message=exc.message,
            status_code=exc.status_code,
            details=exc.details,
        )

    except IntegrityError as exc:
        if commit:
            try:
                _ensure_session(session).rollback()
            except Exception:
                pass

        return _build_error_result(
            code="project_provisioning_integrity_error",
            message="Chunk project provisioning failed because of a database integrity error.",
            status_code=409,
            details={"error": repr(exc)},
        )

    except SQLAlchemyError as exc:
        if commit:
            try:
                _ensure_session(session).rollback()
            except Exception:
                pass

        return _build_error_result(
            code="project_provisioning_database_error",
            message="Chunk project provisioning failed because of a database error.",
            status_code=500,
            details={"error": repr(exc)},
        )

    except Exception as exc:
        if commit:
            try:
                _ensure_session(session).rollback()
            except Exception:
                pass

        return _build_error_result(
            code="project_provisioning_unexpected_error",
            message="Chunk project provisioning failed because of an unexpected error.",
            status_code=500,
            details={"error": repr(exc)},
        )


def ensure_chunk_project_from_payload(
    payload: Mapping[str, Any] | None,
    *,
    session: Any | None = None,
    commit: bool = True,
) -> ChunkProjectProvisioningResult:
    """
    Ensure chunk project using only request payload.

    Useful for POST /projects/ensure.
    """
    data = _payload_dict(payload)
    app_project_public_id = _payload_get(
        data,
        (
            "app_project_public_id",
            "appProjectPublicId",
            "app_project_id",
            "appProjectId",
            "external_app_project_id",
            "externalAppProjectId",
            "external_project_id",
            "externalProjectId",
        ),
        "",
    )

    return ensure_chunk_project_for_app_project(
        _safe_str(app_project_public_id),
        data,
        session=session,
        commit=commit,
    )


def provisioning_result_to_response_tuple(
    result: ChunkProjectProvisioningResult,
) -> tuple[dict[str, Any], int]:
    """
    Convert provisioning result into a Flask-friendly (payload, status_code) tuple.
    """
    return result.to_dict(), result.status_code


__all__ = [
    "PROVISIONING_SCHEMA_VERSION",
    "ProvisioningError",
    "ProvisioningIssue",
    "ChunkProjectProvisioningResult",
    "preview_chunk_project_ids",
    "ensure_chunk_project_for_app_project",
    "ensure_chunk_project_from_payload",
    "provisioning_result_to_response_tuple",
]