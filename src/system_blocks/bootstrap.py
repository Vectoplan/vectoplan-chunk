# services/vectoplan-chunk/src/system_blocks/bootstrap.py
"""
Database reconciliation for built-in VECTOPLAN system blocks.

This module bridges the immutable code-owned system-block catalog with the
persistent ``BlockRegistry`` / ``BlockType`` models of ``vectoplan-chunk``.

The initial reconciliation rules are:

Air
----

``system_air`` is a reserved system-definition identity for the empty cell
state:

    cellValue = 0

Air must never exist as a persistent ``BlockType`` row and must never become a
positive chunk-palette entry.

Railing
-------

``system_railing`` is a persistent built-in system block. Its canonical
definition lives in Python code and is mirrored into the ``BlockRegistry``
assigned to a concrete ``WorldInstance``.

The mirror allows the existing runtime path to remain unchanged:

    SetBlock(system_railing)
    -> resolve BlockType in the world's registry
    -> create/reuse chunk-local palette entry
    -> persist ChunkSnapshot
    -> append ChunkEvent
    -> update WorldCommandLog

Responsibilities
----------------

This module:

- verifies that the target BlockRegistry is persisted and usable,
- verifies that no Air BlockType row exists,
- finds persistent system-block mirrors case-insensitively,
- detects duplicate or conflicting system rows,
- creates missing persistent system-block rows,
- detects drift between code definitions and database rows,
- repairs authoritative system-owned fields,
- restores deleted, disabled or deprecated system-block mirrors when allowed,
- preserves unknown metadata when configured,
- validates resulting BlockType rows,
- supports dry-run planning,
- supports atomic reconciliation through a nested transaction/savepoint,
- flushes when requested,
- returns complete structured diagnostics,
- never commits the outer database transaction.

Important transaction boundary
------------------------------

The caller owns the outer transaction and must explicitly commit or roll back.

This module may use ``db.session.begin_nested()`` to protect reconciliation with
a savepoint, but it never calls:

    db.session.commit()
    db.session.rollback()

This makes the module suitable for integration into the existing explicit
database-bootstrap orchestration.

Important cache boundary
------------------------

Only immutable definitions, descriptors, model-column metadata and expected
persistent values are cached.

Database query results and SQLAlchemy model instances are never cached.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping, Sequence
from contextlib import nullcontext
from dataclasses import dataclass, field, replace
from functools import lru_cache
from types import MappingProxyType
from typing import Any, Final, Optional


# -----------------------------------------------------------------------------
# Optional SQLAlchemy helpers
# -----------------------------------------------------------------------------

try:
    from sqlalchemy import func
except Exception:  # pragma: no cover - SQLAlchemy is required at runtime
    func = None  # type: ignore[assignment]

try:
    from sqlalchemy.orm import noload
except Exception:  # pragma: no cover - defensive test fallback
    noload = None  # type: ignore[assignment]


# -----------------------------------------------------------------------------
# Database and model imports
# -----------------------------------------------------------------------------

try:
    from extensions import db
except Exception as exc:  # pragma: no cover - explicit runtime failure
    db = None  # type: ignore[assignment]
    _DB_IMPORT_ERROR = exc
else:
    _DB_IMPORT_ERROR = None


try:
    from models import BlockRegistry, BlockType
except Exception as exc:  # pragma: no cover - explicit runtime failure
    BlockRegistry = None  # type: ignore[assignment]
    BlockType = None  # type: ignore[assignment]
    _MODEL_IMPORT_ERROR = exc
else:
    _MODEL_IMPORT_ERROR = None


# -----------------------------------------------------------------------------
# System-block imports
# -----------------------------------------------------------------------------

try:
    from .catalog import (
        AIR_SYSTEM_BLOCK_ID,
        SYSTEM_BLOCK_CATALOG_ID,
        SYSTEM_BLOCK_CATALOG_VERSION,
        get_air_system_block_definition,
        get_persistent_system_block_definitions,
        require_system_block_catalog_ready,
        require_system_block_definition_from_catalog,
    )
    from .contracts import (
        AIR_CELL_VALUE,
        SYSTEM_BLOCK_METADATA_NAMESPACE,
        SystemBlockDefinition,
        make_json_safe,
    )
except ImportError:
    try:
        from src.system_blocks.catalog import (
            AIR_SYSTEM_BLOCK_ID,
            SYSTEM_BLOCK_CATALOG_ID,
            SYSTEM_BLOCK_CATALOG_VERSION,
            get_air_system_block_definition,
            get_persistent_system_block_definitions,
            require_system_block_catalog_ready,
            require_system_block_definition_from_catalog,
        )
        from src.system_blocks.contracts import (
            AIR_CELL_VALUE,
            SYSTEM_BLOCK_METADATA_NAMESPACE,
            SystemBlockDefinition,
            make_json_safe,
        )
    except ImportError as exc:
        raise RuntimeError(
            "Could not import the built-in system-block catalog while loading "
            "the database reconciliation module. Ensure the complete "
            "'services/vectoplan-chunk/src/system_blocks' package exists and "
            "the vectoplan-chunk source root is available on PYTHONPATH."
        ) from exc


# -----------------------------------------------------------------------------
# Module constants
# -----------------------------------------------------------------------------

SYSTEM_BLOCK_BOOTSTRAP_MODULE_VERSION: Final[str] = "1.0.0"

SYSTEM_BLOCK_BOOTSTRAP_SCHEMA_VERSION: Final[str] = (
    "system-block-bootstrap-result.schema.v1"
)

SYSTEM_BLOCK_BOOTSTRAP_POLICY_SCHEMA_VERSION: Final[str] = (
    "system-block-bootstrap-policy.schema.v1"
)

SYSTEM_BLOCK_MIRROR_RESULT_SCHEMA_VERSION: Final[str] = (
    "system-block-mirror-result.schema.v1"
)

AIR_INVARIANT_RESULT_SCHEMA_VERSION: Final[str] = (
    "system-block-air-invariant-result.schema.v1"
)

SYSTEM_BLOCK_BOOTSTRAP_SOURCE: Final[str] = (
    "src.system_blocks.bootstrap"
)

SYSTEM_BLOCK_BOOTSTRAP_USER_ID: Final[str] = (
    "vectoplan-system-block-bootstrap"
)

BLOCK_STATUS_ACTIVE: Final[str] = "active"

ACTION_READY: Final[str] = "ready"
ACTION_UNCHANGED: Final[str] = "unchanged"
ACTION_MISSING: Final[str] = "missing"
ACTION_DRIFTED: Final[str] = "drifted"
ACTION_CONFLICT: Final[str] = "conflict"
ACTION_INVALID: Final[str] = "invalid"
ACTION_ERROR: Final[str] = "error"
ACTION_SKIPPED: Final[str] = "skipped"

ACTION_WOULD_CREATE: Final[str] = "would_create"
ACTION_WOULD_UPDATE: Final[str] = "would_update"
ACTION_WOULD_DELETE_ILLEGAL_AIR: Final[str] = (
    "would_delete_illegal_air_rows"
)

ACTION_CREATED: Final[str] = "created"
ACTION_UPDATED: Final[str] = "updated"
ACTION_DELETED_ILLEGAL_AIR: Final[str] = (
    "deleted_illegal_air_rows"
)

ACTION_ROLLED_BACK: Final[str] = "rolled_back"

DEFAULT_QUERY_LIMIT: Final[int] = 3
DEFAULT_FLOAT_TOLERANCE: Final[float] = 1e-9

_MISSING: Final[object] = object()


# -----------------------------------------------------------------------------
# Exceptions
# -----------------------------------------------------------------------------

class SystemBlockBootstrapError(RuntimeError):
    """
    Base error for system-block database reconciliation.
    """


class SystemBlockBootstrapDependencyError(SystemBlockBootstrapError):
    """
    Raised when database or model dependencies are unavailable.
    """


class SystemBlockRegistryError(SystemBlockBootstrapError):
    """
    Raised when the target BlockRegistry cannot be used.
    """


class SystemBlockDuplicateRowError(SystemBlockBootstrapError):
    """
    Raised when multiple rows match one case-insensitive system identity.
    """


class SystemBlockAirInvariantError(SystemBlockBootstrapError):
    """
    Raised when Air exists as an illegal persistent BlockType row.
    """


class SystemBlockMirrorError(SystemBlockBootstrapError):
    """
    Raised when one persistent system-block mirror cannot be reconciled.
    """


class SystemBlockBootstrapNotReadyError(SystemBlockBootstrapError):
    """
    Raised when reconciliation completes without a ready final state.
    """

    def __init__(
        self,
        result: "SystemBlockBootstrapResult",
    ) -> None:
        self.result = result

        errors = result.errors

        details = (
            "; ".join(errors)
            if errors
            else "system-block bootstrap is not ready"
        )

        super().__init__(
            f"System-block bootstrap failed for registry "
            f"'{result.registry_key or '<unknown>'}': {details}"
        )


class _SystemBlockAtomicAbort(RuntimeError):
    """
    Internal exception used only to roll back a nested transaction.

    The exception never escapes the public reconciliation API.
    """

    def __init__(
        self,
        message: str,
        *,
        cause: Optional[BaseException] = None,
    ) -> None:
        self.cause = cause
        super().__init__(message)


# -----------------------------------------------------------------------------
# Policy
# -----------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class SystemBlockBootstrapPolicy:
    """
    Mutation and safety policy for one reconciliation run.

    Defaults are suitable for normal explicit database bootstrap:

    - create missing persistent system-block rows,
    - repair drift,
    - restore inactive system rows,
    - preserve unknown metadata,
    - use a nested transaction,
    - flush changes,
    - do not automatically hard-delete illegal Air rows.

    Deleting illegal Air rows is intentionally opt-in because it is destructive.
    """

    create_missing: bool = True
    update_drifted: bool = True
    restore_inactive: bool = True
    repair_registry_identity: bool = True

    delete_illegal_air_rows: bool = False

    preserve_unknown_metadata: bool = True

    require_active_registry: bool = True
    validate_model_after_write: bool = True

    use_nested_transaction: bool = True
    flush: bool = True
    dry_run: bool = False

    def __post_init__(self) -> None:
        for field_name in (
            "create_missing",
            "update_drifted",
            "restore_inactive",
            "repair_registry_identity",
            "delete_illegal_air_rows",
            "preserve_unknown_metadata",
            "require_active_registry",
            "validate_model_after_write",
            "use_nested_transaction",
            "flush",
            "dry_run",
        ):
            object.__setattr__(
                self,
                field_name,
                _normalize_bool(
                    getattr(self, field_name),
                    default=False,
                ),
            )

    def to_dict(self) -> dict[str, Any]:
        """
        Serialize the policy for diagnostics.
        """
        return {
            "schemaVersion": (
                SYSTEM_BLOCK_BOOTSTRAP_POLICY_SCHEMA_VERSION
            ),
            "createMissing": self.create_missing,
            "updateDrifted": self.update_drifted,
            "restoreInactive": self.restore_inactive,
            "repairRegistryIdentity": (
                self.repair_registry_identity
            ),
            "deleteIllegalAirRows": (
                self.delete_illegal_air_rows
            ),
            "preserveUnknownMetadata": (
                self.preserve_unknown_metadata
            ),
            "requireActiveRegistry": (
                self.require_active_registry
            ),
            "validateModelAfterWrite": (
                self.validate_model_after_write
            ),
            "useNestedTransaction": (
                self.use_nested_transaction
            ),
            "flush": self.flush,
            "dryRun": self.dry_run,
        }


@lru_cache(maxsize=1)
def get_default_system_block_bootstrap_policy(
) -> SystemBlockBootstrapPolicy:
    """
    Return the cached default mutation policy.
    """
    return SystemBlockBootstrapPolicy()


@lru_cache(maxsize=1)
def get_read_only_system_block_bootstrap_policy(
) -> SystemBlockBootstrapPolicy:
    """
    Return the cached non-mutating inspection policy.
    """
    return SystemBlockBootstrapPolicy(
        create_missing=False,
        update_drifted=False,
        restore_inactive=False,
        repair_registry_identity=False,
        delete_illegal_air_rows=False,
        preserve_unknown_metadata=True,
        require_active_registry=True,
        validate_model_after_write=True,
        use_nested_transaction=False,
        flush=False,
        dry_run=True,
    )


@lru_cache(maxsize=1)
def get_repair_system_block_bootstrap_policy(
) -> SystemBlockBootstrapPolicy:
    """
    Return an explicit repair policy.

    This policy also removes illegal Air BlockType rows. It should be used only
    by explicit invariant-repair tooling, not by ordinary request paths.
    """
    return SystemBlockBootstrapPolicy(
        create_missing=True,
        update_drifted=True,
        restore_inactive=True,
        repair_registry_identity=True,
        delete_illegal_air_rows=True,
        preserve_unknown_metadata=True,
        require_active_registry=True,
        validate_model_after_write=True,
        use_nested_transaction=True,
        flush=True,
        dry_run=False,
    )


# -----------------------------------------------------------------------------
# Result structures
# -----------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class AirInvariantResult:
    """
    Result of checking the reserved Air persistence invariant.
    """

    ready: bool
    repairable: bool
    action: str

    registry_db_id: Optional[int]
    registry_id: Optional[str]
    registry_version: Optional[str]

    illegal_row_count: int
    illegal_row_db_ids: tuple[int, ...]
    illegal_block_type_ids: tuple[str, ...]

    would_change: bool = False
    changed: bool = False

    errors: tuple[str, ...] = field(
        default_factory=tuple,
    )

    def to_dict(self) -> dict[str, Any]:
        """
        Serialize Air-invariant diagnostics.
        """
        return {
            "schemaVersion": (
                AIR_INVARIANT_RESULT_SCHEMA_VERSION
            ),
            "ready": self.ready,
            "repairable": self.repairable,
            "action": self.action,
            "registryDbId": self.registry_db_id,
            "registryId": self.registry_id,
            "registryVersion": self.registry_version,
            "registryKey": _build_registry_key(
                self.registry_id,
                self.registry_version,
            ),
            "airCellValue": AIR_CELL_VALUE,
            "systemBlockId": AIR_SYSTEM_BLOCK_ID,
            "illegalRowCount": self.illegal_row_count,
            "illegalRowDbIds": list(
                self.illegal_row_db_ids
            ),
            "illegalBlockTypeIds": list(
                self.illegal_block_type_ids
            ),
            "wouldChange": self.would_change,
            "changed": self.changed,
            "errors": list(self.errors),
        }


@dataclass(frozen=True, slots=True)
class SystemBlockMirrorResult:
    """
    Result for one persistent system-block mirror.
    """

    system_block_id: str
    runtime_block_type_id: Optional[str]

    definition_version: Optional[str]
    definition_fingerprint: Optional[str]

    ready: bool
    repairable: bool
    action: str

    registry_db_id: Optional[int]
    registry_id: Optional[str]
    registry_version: Optional[str]

    block_type_db_id: Optional[int]
    revision_before: Optional[int]
    revision_after: Optional[int]

    drift_before: Mapping[str, Any] = field(
        default_factory=dict,
        repr=False,
    )

    drift_after: Mapping[str, Any] = field(
        default_factory=dict,
        repr=False,
    )

    model_validation_errors: Mapping[str, Any] = field(
        default_factory=dict,
        repr=False,
    )

    would_change: bool = False
    changed: bool = False
    created: bool = False
    updated: bool = False

    errors: tuple[str, ...] = field(
        default_factory=tuple,
    )

    def to_dict(self) -> dict[str, Any]:
        """
        Serialize one mirror result.
        """
        return {
            "schemaVersion": (
                SYSTEM_BLOCK_MIRROR_RESULT_SCHEMA_VERSION
            ),
            "systemBlockId": self.system_block_id,
            "runtimeBlockTypeId": (
                self.runtime_block_type_id
            ),
            "definitionVersion": (
                self.definition_version
            ),
            "definitionFingerprint": (
                self.definition_fingerprint
            ),
            "ready": self.ready,
            "repairable": self.repairable,
            "action": self.action,
            "registryDbId": self.registry_db_id,
            "registryId": self.registry_id,
            "registryVersion": self.registry_version,
            "registryKey": _build_registry_key(
                self.registry_id,
                self.registry_version,
            ),
            "blockTypeDbId": self.block_type_db_id,
            "revisionBefore": self.revision_before,
            "revisionAfter": self.revision_after,
            "driftBefore": make_json_safe(
                self.drift_before
            ),
            "driftAfter": make_json_safe(
                self.drift_after
            ),
            "modelValidationErrors": make_json_safe(
                self.model_validation_errors
            ),
            "wouldChange": self.would_change,
            "changed": self.changed,
            "created": self.created,
            "updated": self.updated,
            "errors": list(self.errors),
        }


@dataclass(frozen=True, slots=True)
class SystemBlockBootstrapResult:
    """
    Complete result of inspecting or reconciling one target registry.
    """

    ready: bool
    repairable: bool

    registry_db_id: Optional[int]
    registry_id: Optional[str]
    registry_version: Optional[str]

    policy: SystemBlockBootstrapPolicy

    air: AirInvariantResult
    mirrors: tuple[SystemBlockMirrorResult, ...]

    dry_run: bool
    changed: bool
    would_change: bool

    nested_transaction_used: bool
    flushed: bool
    rolled_back: bool

    errors: tuple[str, ...] = field(
        default_factory=tuple,
    )

    @property
    def registry_key(self) -> Optional[str]:
        """
        Return the public registry identity.
        """
        return _build_registry_key(
            self.registry_id,
            self.registry_version,
        )

    @property
    def mirror_count(self) -> int:
        return len(self.mirrors)

    @property
    def ready_mirror_count(self) -> int:
        return sum(
            1
            for mirror in self.mirrors
            if mirror.ready
        )

    @property
    def created_count(self) -> int:
        return sum(
            1
            for mirror in self.mirrors
            if mirror.created
        )

    @property
    def updated_count(self) -> int:
        return sum(
            1
            for mirror in self.mirrors
            if mirror.updated
        )

    @property
    def drifted_count(self) -> int:
        return sum(
            1
            for mirror in self.mirrors
            if mirror.drift_before
        )

    def to_dict(self) -> dict[str, Any]:
        """
        Serialize complete bootstrap diagnostics.
        """
        return {
            "schemaVersion": (
                SYSTEM_BLOCK_BOOTSTRAP_SCHEMA_VERSION
            ),
            "ready": self.ready,
            "repairable": self.repairable,
            "source": SYSTEM_BLOCK_BOOTSTRAP_SOURCE,
            "moduleVersion": (
                SYSTEM_BLOCK_BOOTSTRAP_MODULE_VERSION
            ),
            "catalogId": SYSTEM_BLOCK_CATALOG_ID,
            "catalogVersion": (
                SYSTEM_BLOCK_CATALOG_VERSION
            ),
            "registryDbId": self.registry_db_id,
            "registryId": self.registry_id,
            "registryVersion": self.registry_version,
            "registryKey": self.registry_key,
            "policy": self.policy.to_dict(),
            "air": self.air.to_dict(),
            "mirrors": [
                mirror.to_dict()
                for mirror in self.mirrors
            ],
            "counts": {
                "mirrors": self.mirror_count,
                "readyMirrors": self.ready_mirror_count,
                "created": self.created_count,
                "updated": self.updated_count,
                "drifted": self.drifted_count,
            },
            "dryRun": self.dry_run,
            "changed": self.changed,
            "wouldChange": self.would_change,
            "nestedTransactionUsed": (
                self.nested_transaction_used
            ),
            "flushed": self.flushed,
            "rolledBack": self.rolled_back,
            "errors": list(self.errors),
        }

    def require_ready(self) -> None:
        """
        Raise when this result does not represent a ready state.
        """
        if not self.ready:
            raise SystemBlockBootstrapNotReadyError(
                self
            )


# -----------------------------------------------------------------------------
# Dependency validation
# -----------------------------------------------------------------------------

def _require_database_dependencies() -> None:
    """
    Raise clearly when database/model imports are unavailable.
    """
    if db is None:
        raise SystemBlockBootstrapDependencyError(
            "Flask-SQLAlchemy database extension is unavailable."
        ) from _DB_IMPORT_ERROR

    if BlockRegistry is None or BlockType is None:
        raise SystemBlockBootstrapDependencyError(
            "BlockRegistry or BlockType model is unavailable."
        ) from _MODEL_IMPORT_ERROR

    try:
        session = db.session
    except Exception as exc:
        raise SystemBlockBootstrapDependencyError(
            "Database session is unavailable. Ensure an active Flask "
            "application context exists."
        ) from exc

    if session is None:
        raise SystemBlockBootstrapDependencyError(
            "Database session resolved to None."
        )


# -----------------------------------------------------------------------------
# Primitive helpers
# -----------------------------------------------------------------------------

def _safe_exception_text(
    error: BaseException | Any,
) -> str:
    """
    Return a robust exception message.
    """
    try:
        text = str(error).strip()
    except Exception:
        text = ""

    return text or type(error).__name__


def _normalize_bool(
    value: Any,
    *,
    default: bool,
) -> bool:
    """
    Normalize common boolean-like values.
    """
    if value is None:
        return bool(default)

    if isinstance(value, bool):
        return value

    if isinstance(value, int):
        return bool(value)

    try:
        text = str(value).strip().lower()
    except Exception:
        return bool(default)

    if text in {
        "1",
        "true",
        "yes",
        "y",
        "on",
        "enabled",
    }:
        return True

    if text in {
        "0",
        "false",
        "no",
        "n",
        "off",
        "disabled",
    }:
        return False

    return bool(default)


def _normalize_optional_user_id(
    value: Any,
) -> Optional[str]:
    """
    Normalize an optional bootstrap user identifier.
    """
    if value is None:
        return None

    try:
        text = str(value).strip()
    except Exception:
        return None

    return text or None


def _normalize_error_messages(
    errors: Iterable[Any] | Any | None,
) -> tuple[str, ...]:
    """
    Normalize and deduplicate errors while preserving order.
    """
    if errors is None:
        return tuple()

    if isinstance(errors, (str, bytes, bytearray)):
        values = (errors,)
    else:
        try:
            values = tuple(errors)
        except Exception:
            values = (errors,)

    result: list[str] = []
    seen: set[str] = set()

    for error in values:
        try:
            text = str(error).strip()
        except Exception:
            text = type(error).__name__

        if not text or text in seen:
            continue

        seen.add(text)
        result.append(text)

    return tuple(result)


def _build_registry_key(
    registry_id: Optional[str],
    registry_version: Optional[str],
) -> Optional[str]:
    """
    Build a public registry key.
    """
    if not registry_id or not registry_version:
        return None

    return f"{registry_id}@{registry_version}"


def _safe_int(
    value: Any,
) -> Optional[int]:
    """
    Convert a value to int without raising.
    """
    if value is None or isinstance(value, bool):
        return None

    try:
        return int(value)
    except Exception:
        return None


def _safe_text(
    value: Any,
) -> Optional[str]:
    """
    Convert a value to optional normalized text.
    """
    if value is None:
        return None

    try:
        text = str(value).strip()
    except Exception:
        return None

    return text or None


def _safe_getattr(
    value: Any,
    name: str,
    fallback: Any = None,
) -> Any:
    """
    Read an attribute defensively.
    """
    try:
        return getattr(value, name, fallback)
    except Exception:
        return fallback


def _values_equal(
    actual: Any,
    expected: Any,
    *,
    float_tolerance: float = DEFAULT_FLOAT_TOLERANCE,
) -> bool:
    """
    Compare persistent values predictably.
    """
    if isinstance(expected, float):
        try:
            return math.isclose(
                float(actual),
                expected,
                rel_tol=float_tolerance,
                abs_tol=float_tolerance,
            )
        except Exception:
            return False

    return actual == expected


# -----------------------------------------------------------------------------
# Cached static/model helpers
# -----------------------------------------------------------------------------

@lru_cache(maxsize=8)
def _get_model_columns(
    model_class: Any,
) -> frozenset[str]:
    """
    Return cached SQLAlchemy column names for one model class.
    """
    try:
        table = getattr(
            model_class,
            "__table__",
            None,
        )
        columns = getattr(table, "columns", None)

        if columns is None:
            return frozenset()

        return frozenset(
            str(column.name)
            for column in columns
        )
    except Exception:
        return frozenset()


def _model_supports_attribute(
    model_or_instance: Any,
    name: str,
) -> bool:
    """
    Return whether a model supports one persistent attribute.
    """
    model_class = (
        model_or_instance
        if isinstance(model_or_instance, type)
        else type(model_or_instance)
    )

    if name in _get_model_columns(model_class):
        return True

    try:
        return hasattr(model_or_instance, name)
    except Exception:
        return False


@lru_cache(maxsize=1)
def get_system_block_bootstrap_descriptor(
) -> Mapping[str, Any]:
    """
    Return immutable static module diagnostics.
    """
    return MappingProxyType(
        {
            "schemaVersion": (
                SYSTEM_BLOCK_BOOTSTRAP_SCHEMA_VERSION
            ),
            "policySchemaVersion": (
                SYSTEM_BLOCK_BOOTSTRAP_POLICY_SCHEMA_VERSION
            ),
            "moduleVersion": (
                SYSTEM_BLOCK_BOOTSTRAP_MODULE_VERSION
            ),
            "source": SYSTEM_BLOCK_BOOTSTRAP_SOURCE,
            "catalogId": SYSTEM_BLOCK_CATALOG_ID,
            "catalogVersion": (
                SYSTEM_BLOCK_CATALOG_VERSION
            ),
            "airSystemBlockId": AIR_SYSTEM_BLOCK_ID,
            "airCellValue": AIR_CELL_VALUE,
            "commitsTransactions": False,
            "cachesDatabaseRows": False,
            "supportsDryRun": True,
            "supportsNestedTransaction": True,
        }
    )


@lru_cache(maxsize=128)
def _get_expected_persistent_values_cached(
    system_block_id: str,
    definition_fingerprint: str,
    include_metadata: bool,
) -> Mapping[str, Any]:
    """
    Return cached immutable persistent values for one canonical definition.

    ``definition_fingerprint`` is part of the cache key so a later definition
    version cannot accidentally reuse stale expected values.
    """
    definition = (
        require_system_block_definition_from_catalog(
            system_block_id
        )
    )

    if (
        definition.definition_fingerprint
        != definition_fingerprint
    ):
        raise SystemBlockMirrorError(
            f"Definition fingerprint changed while resolving "
            f"'{system_block_id}'."
        )

    values = definition.to_persistent_block_values(
        include_metadata=include_metadata,
    )

    return MappingProxyType(
        dict(values)
    )


def _get_expected_persistent_values(
    definition: SystemBlockDefinition,
    *,
    include_metadata: bool,
) -> Mapping[str, Any]:
    """
    Resolve cached expected persistent values.
    """
    return _get_expected_persistent_values_cached(
        definition.system_block_id,
        definition.definition_fingerprint,
        bool(include_metadata),
    )


# -----------------------------------------------------------------------------
# Registry validation
# -----------------------------------------------------------------------------

def _require_usable_registry(
    registry: Any,
    *,
    policy: SystemBlockBootstrapPolicy,
) -> Any:
    """
    Validate and return a persisted target BlockRegistry.
    """
    _require_database_dependencies()

    if registry is None:
        raise SystemBlockRegistryError(
            "BlockRegistry is required."
        )

    if BlockRegistry is not None and not isinstance(
        registry,
        BlockRegistry,
    ):
        raise SystemBlockRegistryError(
            "registry must be a BlockRegistry instance."
        )

    registry_db_id = _safe_int(
        _safe_getattr(registry, "id")
    )

    registry_id = _safe_text(
        _safe_getattr(registry, "registry_id")
    )

    registry_version = _safe_text(
        _safe_getattr(
            registry,
            "registry_version",
        )
    )

    if registry_db_id is None or registry_db_id <= 0:
        raise SystemBlockRegistryError(
            "BlockRegistry must be persisted and have a positive id."
        )

    if not registry_id:
        raise SystemBlockRegistryError(
            "BlockRegistry.registry_id is required."
        )

    if not registry_version:
        raise SystemBlockRegistryError(
            "BlockRegistry.registry_version is required."
        )

    if policy.require_active_registry:
        is_deleted = bool(
            _safe_getattr(
                registry,
                "is_deleted",
                False,
            )
        )

        status = (
            _safe_text(
                _safe_getattr(
                    registry,
                    "status",
                )
            )
            or ""
        ).lower()

        if is_deleted:
            raise SystemBlockRegistryError(
                f"Block registry "
                f"'{registry_id}@{registry_version}' is deleted."
            )

        if status and status != BLOCK_STATUS_ACTIVE:
            raise SystemBlockRegistryError(
                f"Block registry "
                f"'{registry_id}@{registry_version}' is not active."
            )

    return registry


# -----------------------------------------------------------------------------
# Query helpers
# -----------------------------------------------------------------------------

def _query_without_relationships(
    query: Any,
) -> Any:
    """
    Disable ORM relationship loading for bootstrap lookups.
    """
    if noload is None:
        return query

    try:
        return query.options(noload("*"))
    except Exception:
        return query


def _query_block_rows(
    *,
    registry: Any,
    block_type_id: str,
    limit: int = DEFAULT_QUERY_LIMIT,
) -> tuple[Any, ...]:
    """
    Query one system identity case-insensitively inside one registry.

    Case-insensitive lookup prevents a row such as ``SYSTEM_RAILING`` from
    coexisting unnoticed with the canonical ``system_railing`` identity.
    """
    _require_database_dependencies()

    registry_db_id = _safe_int(
        _safe_getattr(registry, "id")
    )

    if registry_db_id is None:
        raise SystemBlockRegistryError(
            "Cannot query BlockType rows without registry.id."
        )

    normalized_id = str(block_type_id).strip()

    if not normalized_id:
        raise ValueError(
            "block_type_id is required."
        )

    query = BlockType.query.filter(
        BlockType.registry_db_id
        == registry_db_id
    )

    if func is not None:
        query = query.filter(
            func.lower(BlockType.block_type_id)
            == normalized_id.lower()
        )
    else:
        query = query.filter(
            BlockType.block_type_id
            == normalized_id
        )

    query = _query_without_relationships(
        query
    )

    try:
        rows = query.limit(
            max(2, int(limit))
        ).all()
    except Exception as exc:
        raise SystemBlockBootstrapError(
            f"Database lookup failed for BlockType "
            f"'{normalized_id}' in registry "
            f"'{_safe_getattr(registry, 'registry_id')}@"
            f"{_safe_getattr(registry, 'registry_version')}'."
        ) from exc

    return tuple(rows)


def find_system_block_mirror_rows(
    registry: Any,
    identifier: str,
) -> tuple[Any, ...]:
    """
    Public read-only lookup for system BlockType rows.

    Database rows are returned directly and are deliberately not cached.
    """
    policy = (
        get_read_only_system_block_bootstrap_policy()
    )

    resolved_registry = _require_usable_registry(
        registry,
        policy=policy,
    )

    return _query_block_rows(
        registry=resolved_registry,
        block_type_id=identifier,
    )


# -----------------------------------------------------------------------------
# Metadata helpers
# -----------------------------------------------------------------------------

def _normalize_mapping(
    value: Any,
) -> dict[str, Any]:
    """
    Convert a mapping-like value into a JSON-safe dictionary.
    """
    if not isinstance(value, Mapping):
        return {}

    result = make_json_safe(dict(value))

    if isinstance(result, dict):
        return result

    return {}


def _deep_merge_metadata(
    existing: Mapping[str, Any] | None,
    expected: Mapping[str, Any] | None,
    *,
    preserve_unknown: bool,
) -> dict[str, Any]:
    """
    Merge authoritative expected metadata into existing metadata.

    Expected values always win. Unknown existing values are preserved only when
    ``preserve_unknown`` is enabled.
    """
    existing_dict = _normalize_mapping(
        existing
    )

    expected_dict = _normalize_mapping(
        expected
    )

    if preserve_unknown:
        result = dict(existing_dict)
    else:
        result = {}

    for key, expected_value in expected_dict.items():
        existing_value = result.get(key)

        if (
            isinstance(existing_value, Mapping)
            and isinstance(expected_value, Mapping)
        ):
            result[key] = _deep_merge_metadata(
                existing_value,
                expected_value,
                preserve_unknown=preserve_unknown,
            )
        else:
            result[key] = make_json_safe(
                expected_value
            )

    return result


def _collect_metadata_subset_drift(
    *,
    expected: Mapping[str, Any],
    actual: Mapping[str, Any],
    path: str = "metadata_json",
) -> dict[str, dict[str, Any]]:
    """
    Return drift for every expected metadata value.

    Unknown actual metadata does not count as drift.
    """
    drift: dict[str, dict[str, Any]] = {}

    for key, expected_value in expected.items():
        actual_value = actual.get(
            key,
            _MISSING,
        )

        item_path = f"{path}.{key}"

        if (
            isinstance(expected_value, Mapping)
            and isinstance(actual_value, Mapping)
        ):
            drift.update(
                _collect_metadata_subset_drift(
                    expected=expected_value,
                    actual=actual_value,
                    path=item_path,
                )
            )
            continue

        if actual_value is _MISSING:
            drift[item_path] = {
                "expected": make_json_safe(
                    expected_value
                ),
                "actual": "<missing>",
            }
            continue

        if make_json_safe(actual_value) != make_json_safe(
            expected_value
        ):
            drift[item_path] = {
                "expected": make_json_safe(
                    expected_value
                ),
                "actual": make_json_safe(
                    actual_value
                ),
            }

    return drift


# -----------------------------------------------------------------------------
# Row serialization and model validation
# -----------------------------------------------------------------------------

def _serialize_block_row(
    row: Any,
) -> dict[str, Any]:
    """
    Serialize a shallow BlockType identity for diagnostics.
    """
    return {
        "id": _safe_int(
            _safe_getattr(row, "id")
        ),
        "registryDbId": _safe_int(
            _safe_getattr(
                row,
                "registry_db_id",
            )
        ),
        "registryId": _safe_text(
            _safe_getattr(
                row,
                "registry_id",
            )
        ),
        "registryVersion": _safe_text(
            _safe_getattr(
                row,
                "registry_version",
            )
        ),
        "blockTypeId": _safe_text(
            _safe_getattr(
                row,
                "block_type_id",
            )
        ),
        "status": _safe_text(
            _safe_getattr(row, "status")
        ),
        "revision": _safe_int(
            _safe_getattr(row, "revision")
        ),
        "deleted": bool(
            _safe_getattr(
                row,
                "is_deleted",
                False,
            )
        ),
    }


def _get_model_validation_errors(
    row: Any,
) -> Mapping[str, Any]:
    """
    Return model-level validation errors without raising.
    """
    validation_method = _safe_getattr(
        row,
        "get_validation_errors",
    )

    if not callable(validation_method):
        return MappingProxyType({})

    try:
        raw_errors = validation_method()
    except Exception as exc:
        return MappingProxyType(
            {
                "_validation": (
                    f"{type(exc).__name__}: "
                    f"{_safe_exception_text(exc)}"
                )
            }
        )

    if not isinstance(raw_errors, Mapping):
        return MappingProxyType(
            {
                "_validation": (
                    "get_validation_errors() did not return "
                    "a mapping."
                )
            }
        )

    return MappingProxyType(
        _normalize_mapping(raw_errors)
    )


# -----------------------------------------------------------------------------
# Drift detection
# -----------------------------------------------------------------------------

def _compare_block_type_with_definition(
    *,
    registry: Any,
    block_type: Any,
    definition: SystemBlockDefinition,
) -> Mapping[str, Any]:
    """
    Compare one persistent row with the canonical system definition.
    """
    expected_values = (
        _get_expected_persistent_values(
            definition,
            include_metadata=False,
        )
    )

    drift: dict[str, Any] = {}

    for attribute_name, expected_value in (
        expected_values.items()
    ):
        if attribute_name == "metadata_json":
            continue

        actual_value = _safe_getattr(
            block_type,
            attribute_name,
            _MISSING,
        )

        if actual_value is _MISSING:
            drift[attribute_name] = {
                "expected": make_json_safe(
                    expected_value
                ),
                "actual": "<missing>",
            }
            continue

        if not _values_equal(
            actual_value,
            expected_value,
        ):
            drift[attribute_name] = {
                "expected": make_json_safe(
                    expected_value
                ),
                "actual": make_json_safe(
                    actual_value
                ),
            }

    registry_expectations = {
        "registry_db_id": _safe_int(
            _safe_getattr(registry, "id")
        ),
        "registry_id": _safe_text(
            _safe_getattr(
                registry,
                "registry_id",
            )
        ),
        "registry_version": _safe_text(
            _safe_getattr(
                registry,
                "registry_version",
            )
        ),
    }

    for attribute_name, expected_value in (
        registry_expectations.items()
    ):
        actual_value = _safe_getattr(
            block_type,
            attribute_name,
            _MISSING,
        )

        if actual_value is _MISSING:
            drift[attribute_name] = {
                "expected": expected_value,
                "actual": "<missing>",
            }
        elif actual_value != expected_value:
            drift[attribute_name] = {
                "expected": expected_value,
                "actual": make_json_safe(
                    actual_value
                ),
            }

    # Active system rows must not retain soft-delete/deprecation timestamps.
    for timestamp_name in (
        "deleted_at",
        "deprecated_at",
    ):
        if not _model_supports_attribute(
            block_type,
            timestamp_name,
        ):
            continue

        actual_timestamp = _safe_getattr(
            block_type,
            timestamp_name,
        )

        if actual_timestamp is not None:
            drift[timestamp_name] = {
                "expected": None,
                "actual": make_json_safe(
                    actual_timestamp
                ),
            }

    expected_metadata = (
        _get_expected_persistent_values(
            definition,
            include_metadata=True,
        ).get("metadata_json", {})
    )

    actual_metadata = _normalize_mapping(
        _safe_getattr(
            block_type,
            "metadata_json",
            {},
        )
    )

    if isinstance(expected_metadata, Mapping):
        drift.update(
            _collect_metadata_subset_drift(
                expected=expected_metadata,
                actual=actual_metadata,
            )
        )

    return MappingProxyType(drift)


# -----------------------------------------------------------------------------
# Air inspection
# -----------------------------------------------------------------------------

def _inspect_air_invariant(
    *,
    registry: Any,
    policy: SystemBlockBootstrapPolicy,
) -> AirInvariantResult:
    """
    Inspect whether Air incorrectly exists as a BlockType row.
    """
    registry_db_id = _safe_int(
        _safe_getattr(registry, "id")
    )

    registry_id = _safe_text(
        _safe_getattr(
            registry,
            "registry_id",
        )
    )

    registry_version = _safe_text(
        _safe_getattr(
            registry,
            "registry_version",
        )
    )

    errors: list[str] = []

    try:
        air_definition = (
            get_air_system_block_definition()
        )
    except Exception as exc:
        errors.append(
            "Could not load canonical Air definition: "
            f"{type(exc).__name__}: "
            f"{_safe_exception_text(exc)}"
        )

        return AirInvariantResult(
            ready=False,
            repairable=False,
            action=ACTION_ERROR,
            registry_db_id=registry_db_id,
            registry_id=registry_id,
            registry_version=registry_version,
            illegal_row_count=0,
            illegal_row_db_ids=tuple(),
            illegal_block_type_ids=tuple(),
            would_change=False,
            changed=False,
            errors=_normalize_error_messages(
                errors
            ),
        )

    if (
        not air_definition.is_air_state
        or air_definition.reserved_cell_value
        != AIR_CELL_VALUE
    ):
        errors.append(
            "Canonical Air definition does not satisfy "
            "the reserved cell-value invariant."
        )

    try:
        rows = _query_block_rows(
            registry=registry,
            block_type_id=(
                air_definition.system_block_id
            ),
        )
    except Exception as exc:
        errors.append(
            "Could not inspect Air BlockType rows: "
            f"{type(exc).__name__}: "
            f"{_safe_exception_text(exc)}"
        )

        return AirInvariantResult(
            ready=False,
            repairable=False,
            action=ACTION_ERROR,
            registry_db_id=registry_db_id,
            registry_id=registry_id,
            registry_version=registry_version,
            illegal_row_count=0,
            illegal_row_db_ids=tuple(),
            illegal_block_type_ids=tuple(),
            would_change=False,
            changed=False,
            errors=_normalize_error_messages(
                errors
            ),
        )

    row_ids = tuple(
        row_id
        for row_id in (
            _safe_int(
                _safe_getattr(row, "id")
            )
            for row in rows
        )
        if row_id is not None
    )

    block_type_ids = tuple(
        block_type_id
        for block_type_id in (
            _safe_text(
                _safe_getattr(
                    row,
                    "block_type_id",
                )
            )
            for row in rows
        )
        if block_type_id
    )

    if rows:
        errors.append(
            f"Air must not be persisted as BlockType; found "
            f"{len(rows)} illegal row(s)."
        )

        repairable = bool(
            policy.delete_illegal_air_rows
        )

        action = (
            ACTION_WOULD_DELETE_ILLEGAL_AIR
            if repairable
            else ACTION_INVALID
        )

        return AirInvariantResult(
            ready=False,
            repairable=repairable,
            action=action,
            registry_db_id=registry_db_id,
            registry_id=registry_id,
            registry_version=registry_version,
            illegal_row_count=len(rows),
            illegal_row_db_ids=row_ids,
            illegal_block_type_ids=(
                block_type_ids
            ),
            would_change=repairable,
            changed=False,
            errors=_normalize_error_messages(
                errors
            ),
        )

    return AirInvariantResult(
        ready=not errors,
        repairable=not errors,
        action=(
            ACTION_READY
            if not errors
            else ACTION_INVALID
        ),
        registry_db_id=registry_db_id,
        registry_id=registry_id,
        registry_version=registry_version,
        illegal_row_count=0,
        illegal_row_db_ids=tuple(),
        illegal_block_type_ids=tuple(),
        would_change=False,
        changed=False,
        errors=_normalize_error_messages(
            errors
        ),
    )


# -----------------------------------------------------------------------------
# Persistent mirror inspection
# -----------------------------------------------------------------------------

def _inspect_persistent_definition(
    *,
    registry: Any,
    definition: SystemBlockDefinition,
    policy: SystemBlockBootstrapPolicy,
) -> SystemBlockMirrorResult:
    """
    Inspect one persistent system-block mirror without mutating it.
    """
    registry_db_id = _safe_int(
        _safe_getattr(registry, "id")
    )

    registry_id = _safe_text(
        _safe_getattr(
            registry,
            "registry_id",
        )
    )

    registry_version = _safe_text(
        _safe_getattr(
            registry,
            "registry_version",
        )
    )

    runtime_id = (
        definition.runtime_block_type_id
    )

    if not runtime_id:
        return SystemBlockMirrorResult(
            system_block_id=(
                definition.system_block_id
            ),
            runtime_block_type_id=None,
            definition_version=(
                definition.definition_version
            ),
            definition_fingerprint=(
                definition.definition_fingerprint
            ),
            ready=False,
            repairable=False,
            action=ACTION_INVALID,
            registry_db_id=registry_db_id,
            registry_id=registry_id,
            registry_version=registry_version,
            block_type_db_id=None,
            revision_before=None,
            revision_after=None,
            errors=(
                "Persistent system definition has no "
                "runtime_block_type_id.",
            ),
        )

    try:
        rows = _query_block_rows(
            registry=registry,
            block_type_id=runtime_id,
        )
    except Exception as exc:
        return SystemBlockMirrorResult(
            system_block_id=(
                definition.system_block_id
            ),
            runtime_block_type_id=runtime_id,
            definition_version=(
                definition.definition_version
            ),
            definition_fingerprint=(
                definition.definition_fingerprint
            ),
            ready=False,
            repairable=False,
            action=ACTION_ERROR,
            registry_db_id=registry_db_id,
            registry_id=registry_id,
            registry_version=registry_version,
            block_type_db_id=None,
            revision_before=None,
            revision_after=None,
            errors=(
                "Could not query persistent system-block mirror: "
                f"{type(exc).__name__}: "
                f"{_safe_exception_text(exc)}",
            ),
        )

    if len(rows) > 1:
        row_summary = [
            _serialize_block_row(row)
            for row in rows
        ]

        return SystemBlockMirrorResult(
            system_block_id=(
                definition.system_block_id
            ),
            runtime_block_type_id=runtime_id,
            definition_version=(
                definition.definition_version
            ),
            definition_fingerprint=(
                definition.definition_fingerprint
            ),
            ready=False,
            repairable=False,
            action=ACTION_CONFLICT,
            registry_db_id=registry_db_id,
            registry_id=registry_id,
            registry_version=registry_version,
            block_type_db_id=None,
            revision_before=None,
            revision_after=None,
            drift_before=MappingProxyType(
                {
                    "duplicateRows": {
                        "expected": 1,
                        "actual": len(rows),
                        "rows": row_summary,
                    }
                }
            ),
            errors=(
                f"Multiple case-insensitive BlockType rows match "
                f"'{runtime_id}'.",
            ),
        )

    if not rows:
        repairable = bool(
            policy.create_missing
        )

        return SystemBlockMirrorResult(
            system_block_id=(
                definition.system_block_id
            ),
            runtime_block_type_id=runtime_id,
            definition_version=(
                definition.definition_version
            ),
            definition_fingerprint=(
                definition.definition_fingerprint
            ),
            ready=False,
            repairable=repairable,
            action=(
                ACTION_WOULD_CREATE
                if repairable
                else ACTION_MISSING
            ),
            registry_db_id=registry_db_id,
            registry_id=registry_id,
            registry_version=registry_version,
            block_type_db_id=None,
            revision_before=None,
            revision_after=None,
            would_change=repairable,
            errors=(
                ()
                if repairable
                else (
                    f"Persistent system block "
                    f"'{runtime_id}' is missing.",
                )
            ),
        )

    row = rows[0]

    drift = _compare_block_type_with_definition(
        registry=registry,
        block_type=row,
        definition=definition,
    )

    model_validation_errors = (
        _get_model_validation_errors(row)
        if policy.validate_model_after_write
        else MappingProxyType({})
    )

    row_status = (
        _safe_text(
            _safe_getattr(row, "status")
        )
        or ""
    ).lower()

    row_is_deleted = bool(
        _safe_getattr(
            row,
            "is_deleted",
            False,
        )
    )

    inactive = bool(
        row_is_deleted
        or row_status != BLOCK_STATUS_ACTIVE
    )

    errors: list[str] = []

    if inactive and not policy.restore_inactive:
        errors.append(
            f"Persistent system block '{runtime_id}' is inactive "
            "and restore_inactive is disabled."
        )

    registry_identity_drift = any(
        key in drift
        for key in (
            "registry_db_id",
            "registry_id",
            "registry_version",
        )
    )

    if (
        registry_identity_drift
        and not policy.repair_registry_identity
    ):
        errors.append(
            f"Persistent system block '{runtime_id}' has registry "
            "identity drift and repair_registry_identity is disabled."
        )

    if model_validation_errors:
        # Validation errors may themselves be repaired by the canonical update.
        # They therefore do not automatically make the state unrepairable.
        pass

    if not drift and not model_validation_errors:
        return SystemBlockMirrorResult(
            system_block_id=(
                definition.system_block_id
            ),
            runtime_block_type_id=runtime_id,
            definition_version=(
                definition.definition_version
            ),
            definition_fingerprint=(
                definition.definition_fingerprint
            ),
            ready=True,
            repairable=True,
            action=ACTION_UNCHANGED,
            registry_db_id=registry_db_id,
            registry_id=registry_id,
            registry_version=registry_version,
            block_type_db_id=_safe_int(
                _safe_getattr(row, "id")
            ),
            revision_before=_safe_int(
                _safe_getattr(
                    row,
                    "revision",
                )
            ),
            revision_after=_safe_int(
                _safe_getattr(
                    row,
                    "revision",
                )
            ),
            drift_before=MappingProxyType({}),
            drift_after=MappingProxyType({}),
            model_validation_errors=(
                MappingProxyType({})
            ),
            would_change=False,
            changed=False,
            errors=tuple(),
        )

    repairable = bool(
        policy.update_drifted
        and (
            not inactive
            or policy.restore_inactive
        )
        and (
            not registry_identity_drift
            or policy.repair_registry_identity
        )
    )

    if not policy.update_drifted:
        errors.append(
            f"Persistent system block '{runtime_id}' has drift "
            "and update_drifted is disabled."
        )

    return SystemBlockMirrorResult(
        system_block_id=(
            definition.system_block_id
        ),
        runtime_block_type_id=runtime_id,
        definition_version=(
            definition.definition_version
        ),
        definition_fingerprint=(
            definition.definition_fingerprint
        ),
        ready=False,
        repairable=repairable,
        action=(
            ACTION_WOULD_UPDATE
            if repairable
            else ACTION_DRIFTED
        ),
        registry_db_id=registry_db_id,
        registry_id=registry_id,
        registry_version=registry_version,
        block_type_db_id=_safe_int(
            _safe_getattr(row, "id")
        ),
        revision_before=_safe_int(
            _safe_getattr(
                row,
                "revision",
            )
        ),
        revision_after=_safe_int(
            _safe_getattr(
                row,
                "revision",
            )
        ),
        drift_before=drift,
        drift_after=drift,
        model_validation_errors=(
            model_validation_errors
        ),
        would_change=repairable,
        changed=False,
        errors=_normalize_error_messages(
            errors
        ),
    )


# -----------------------------------------------------------------------------
# Aggregate inspection
# -----------------------------------------------------------------------------

def inspect_system_blocks_for_registry(
    registry: Any,
    *,
    policy: Optional[
        SystemBlockBootstrapPolicy
    ] = None,
) -> SystemBlockBootstrapResult:
    """
    Inspect Air and all persistent system mirrors without mutation.

    A supplied mutation policy influences whether detected problems are reported
    as repairable, but this function never writes to the database.
    """
    resolved_policy = (
        policy
        or get_default_system_block_bootstrap_policy()
    )

    resolved_registry = _require_usable_registry(
        registry,
        policy=resolved_policy,
    )

    try:
        require_system_block_catalog_ready()
    except Exception as exc:
        raise SystemBlockBootstrapError(
            "System-block catalog is not ready."
        ) from exc

    air_result = _inspect_air_invariant(
        registry=resolved_registry,
        policy=resolved_policy,
    )

    mirror_results: list[
        SystemBlockMirrorResult
    ] = []

    for definition in (
        get_persistent_system_block_definitions()
    ):
        mirror_results.append(
            _inspect_persistent_definition(
                registry=resolved_registry,
                definition=definition,
                policy=resolved_policy,
            )
        )

    errors: list[Any] = []

    if not air_result.ready:
        errors.extend(air_result.errors)

    for mirror in mirror_results:
        if not mirror.ready:
            errors.extend(mirror.errors)

    ready = bool(
        air_result.ready
        and all(
            mirror.ready
            for mirror in mirror_results
        )
    )

    repairable = bool(
        air_result.repairable
        and all(
            mirror.repairable
            for mirror in mirror_results
        )
    )

    would_change = bool(
        air_result.would_change
        or any(
            mirror.would_change
            for mirror in mirror_results
        )
    )

    return SystemBlockBootstrapResult(
        ready=ready,
        repairable=repairable,
        registry_db_id=_safe_int(
            _safe_getattr(
                resolved_registry,
                "id",
            )
        ),
        registry_id=_safe_text(
            _safe_getattr(
                resolved_registry,
                "registry_id",
            )
        ),
        registry_version=_safe_text(
            _safe_getattr(
                resolved_registry,
                "registry_version",
            )
        ),
        policy=resolved_policy,
        air=air_result,
        mirrors=tuple(mirror_results),
        dry_run=True,
        changed=False,
        would_change=would_change,
        nested_transaction_used=False,
        flushed=False,
        rolled_back=False,
        errors=_normalize_error_messages(
            errors
        ),
    )


# -----------------------------------------------------------------------------
# Mutation helpers
# -----------------------------------------------------------------------------

def _flush_session() -> None:
    """
    Flush the current SQLAlchemy session without committing.
    """
    _require_database_dependencies()

    try:
        db.session.flush()
    except Exception as exc:
        raise SystemBlockBootstrapError(
            "Could not flush system-block reconciliation changes."
        ) from exc


def _apply_air_repair(
    *,
    registry: Any,
    policy: SystemBlockBootstrapPolicy,
) -> int:
    """
    Hard-delete illegal Air BlockType rows.

    This operation is executed only when explicitly enabled by policy.
    """
    if not policy.delete_illegal_air_rows:
        raise SystemBlockAirInvariantError(
            "Deleting illegal Air rows is disabled."
        )

    rows = _query_block_rows(
        registry=registry,
        block_type_id=AIR_SYSTEM_BLOCK_ID,
    )

    deleted_count = 0

    for row in rows:
        try:
            db.session.delete(row)
        except Exception as exc:
            raise SystemBlockAirInvariantError(
                "Could not delete illegal Air BlockType row "
                f"{_safe_getattr(row, 'id')!r}."
            ) from exc

        deleted_count += 1

    return deleted_count


def _set_attribute_if_changed(
    target: Any,
    attribute_name: str,
    expected_value: Any,
) -> bool:
    """
    Set one supported attribute and return whether it changed.
    """
    if not _model_supports_attribute(
        target,
        attribute_name,
    ):
        raise SystemBlockMirrorError(
            f"BlockType model does not support required attribute "
            f"'{attribute_name}'."
        )

    current_value = _safe_getattr(
        target,
        attribute_name,
        _MISSING,
    )

    if (
        current_value is not _MISSING
        and _values_equal(
            current_value,
            expected_value,
        )
    ):
        return False

    try:
        setattr(
            target,
            attribute_name,
            expected_value,
        )
    except Exception as exc:
        raise SystemBlockMirrorError(
            f"Could not set BlockType.{attribute_name}."
        ) from exc

    return True


def _touch_block_type_once(
    block_type: Any,
    *,
    updated_by_user_id: Optional[str],
) -> None:
    """
    Increment revision and update timestamps exactly once.

    The model's own ``touch`` method is preferred.
    """
    touch = _safe_getattr(
        block_type,
        "touch",
    )

    if callable(touch):
        try:
            touch(
                updated_by_user_id=(
                    updated_by_user_id
                )
            )
            return
        except Exception as exc:
            raise SystemBlockMirrorError(
                "Could not touch updated system BlockType."
            ) from exc

    # Defensive fallback for reduced test doubles.
    if _model_supports_attribute(
        block_type,
        "revision",
    ):
        current_revision = (
            _safe_int(
                _safe_getattr(
                    block_type,
                    "revision",
                )
            )
            or 0
        )
        setattr(
            block_type,
            "revision",
            current_revision + 1,
        )

    if (
        updated_by_user_id
        and _model_supports_attribute(
            block_type,
            "updated_by_user_id",
        )
    ):
        setattr(
            block_type,
            "updated_by_user_id",
            updated_by_user_id,
        )


def _create_persistent_system_block(
    *,
    registry: Any,
    definition: SystemBlockDefinition,
    created_by_user_id: Optional[str],
) -> Any:
    """
    Create and add one missing persistent system BlockType.
    """
    values = dict(
        _get_expected_persistent_values(
            definition,
            include_metadata=True,
        )
    )

    values["created_by_user_id"] = (
        created_by_user_id
    )

    create_for_registry = _safe_getattr(
        BlockType,
        "create_for_registry",
    )

    if not callable(create_for_registry):
        raise SystemBlockMirrorError(
            "BlockType.create_for_registry() is unavailable."
        )

    try:
        block_type = create_for_registry(
            registry,
            **values,
        )
    except Exception as exc:
        raise SystemBlockMirrorError(
            f"Could not create persistent system block "
            f"'{definition.runtime_block_type_id}'."
        ) from exc

    try:
        db.session.add(block_type)
    except Exception as exc:
        raise SystemBlockMirrorError(
            f"Could not add persistent system block "
            f"'{definition.runtime_block_type_id}' to the session."
        ) from exc

    return block_type


def _update_persistent_system_block(
    *,
    registry: Any,
    block_type: Any,
    definition: SystemBlockDefinition,
    policy: SystemBlockBootstrapPolicy,
    updated_by_user_id: Optional[str],
) -> bool:
    """
    Apply canonical system-owned values to one existing BlockType.
    """
    expected_values = dict(
        _get_expected_persistent_values(
            definition,
            include_metadata=False,
        )
    )

    changed = False

    for attribute_name, expected_value in (
        expected_values.items()
    ):
        if attribute_name == "metadata_json":
            continue

        changed = (
            _set_attribute_if_changed(
                block_type,
                attribute_name,
                expected_value,
            )
            or changed
        )

    if policy.repair_registry_identity:
        registry_values = {
            "registry_db_id": _safe_int(
                _safe_getattr(registry, "id")
            ),
            "registry_id": _safe_text(
                _safe_getattr(
                    registry,
                    "registry_id",
                )
            ),
            "registry_version": _safe_text(
                _safe_getattr(
                    registry,
                    "registry_version",
                )
            ),
        }

        for attribute_name, expected_value in (
            registry_values.items()
        ):
            changed = (
                _set_attribute_if_changed(
                    block_type,
                    attribute_name,
                    expected_value,
                )
                or changed
            )

    expected_metadata = (
        _get_expected_persistent_values(
            definition,
            include_metadata=True,
        ).get("metadata_json", {})
    )

    existing_metadata = _normalize_mapping(
        _safe_getattr(
            block_type,
            "metadata_json",
            {},
        )
    )

    merged_metadata = _deep_merge_metadata(
        existing_metadata,
        (
            expected_metadata
            if isinstance(
                expected_metadata,
                Mapping,
            )
            else {}
        ),
        preserve_unknown=(
            policy.preserve_unknown_metadata
        ),
    )

    if existing_metadata != merged_metadata:
        changed = (
            _set_attribute_if_changed(
                block_type,
                "metadata_json",
                merged_metadata,
            )
            or changed
        )

    # A restored active system block must not retain lifecycle tombstones.
    if policy.restore_inactive:
        for timestamp_name in (
            "deleted_at",
            "deprecated_at",
        ):
            if not _model_supports_attribute(
                block_type,
                timestamp_name,
            ):
                continue

            if (
                _safe_getattr(
                    block_type,
                    timestamp_name,
                )
                is not None
            ):
                setattr(
                    block_type,
                    timestamp_name,
                    None,
                )
                changed = True

    if changed:
        _touch_block_type_once(
            block_type,
            updated_by_user_id=(
                updated_by_user_id
            ),
        )

    return changed


def _apply_mirror_plan(
    *,
    registry: Any,
    plan: SystemBlockMirrorResult,
    policy: SystemBlockBootstrapPolicy,
    created_by_user_id: Optional[str],
    updated_by_user_id: Optional[str],
) -> tuple[Any, str, bool, bool]:
    """
    Apply one inspected mirror action.

    Returns:

        row
        action
        created
        updated
    """
    definition = (
        require_system_block_definition_from_catalog(
            plan.system_block_id
        )
    )

    runtime_id = (
        definition.runtime_block_type_id
    )

    if not runtime_id:
        raise SystemBlockMirrorError(
            f"System definition "
            f"'{definition.system_block_id}' has no runtime ID."
        )

    rows = _query_block_rows(
        registry=registry,
        block_type_id=runtime_id,
    )

    if len(rows) > 1:
        raise SystemBlockDuplicateRowError(
            f"Multiple case-insensitive rows match "
            f"'{runtime_id}'."
        )

    if not rows:
        if not policy.create_missing:
            raise SystemBlockMirrorError(
                f"Persistent system block "
                f"'{runtime_id}' is missing and creation is disabled."
            )

        row = _create_persistent_system_block(
            registry=registry,
            definition=definition,
            created_by_user_id=(
                created_by_user_id
            ),
        )

        return (
            row,
            ACTION_CREATED,
            True,
            False,
        )

    row = rows[0]

    drift = _compare_block_type_with_definition(
        registry=registry,
        block_type=row,
        definition=definition,
    )

    if not drift:
        return (
            row,
            ACTION_UNCHANGED,
            False,
            False,
        )

    if not policy.update_drifted:
        raise SystemBlockMirrorError(
            f"Persistent system block "
            f"'{runtime_id}' has drift and updates are disabled."
        )

    updated = _update_persistent_system_block(
        registry=registry,
        block_type=row,
        definition=definition,
        policy=policy,
        updated_by_user_id=(
            updated_by_user_id
        ),
    )

    return (
        row,
        (
            ACTION_UPDATED
            if updated
            else ACTION_UNCHANGED
        ),
        False,
        updated,
    )


# -----------------------------------------------------------------------------
# Reconciliation
# -----------------------------------------------------------------------------

def _build_transaction_context(
    policy: SystemBlockBootstrapPolicy,
) -> Any:
    """
    Build the requested transaction context.

    A nested transaction creates a savepoint but does not commit the caller's
    outer transaction.
    """
    if not policy.use_nested_transaction:
        return nullcontext()

    try:
        return db.session.begin_nested()
    except Exception as exc:
        raise SystemBlockBootstrapError(
            "Could not start nested system-block transaction."
        ) from exc


def _combine_final_mirror_result(
    *,
    plan: SystemBlockMirrorResult,
    final: SystemBlockMirrorResult,
    action: str,
    created: bool,
    updated: bool,
) -> SystemBlockMirrorResult:
    """
    Combine final readiness with the action performed.
    """
    return replace(
        final,
        action=action,
        revision_before=(
            plan.revision_before
        ),
        revision_after=(
            final.revision_after
        ),
        drift_before=(
            plan.drift_before
        ),
        would_change=(
            plan.would_change
        ),
        changed=bool(created or updated),
        created=created,
        updated=updated,
    )


def reconcile_system_blocks_for_registry(
    registry: Any,
    *,
    policy: Optional[
        SystemBlockBootstrapPolicy
    ] = None,
    created_by_user_id: Optional[str] = (
        SYSTEM_BLOCK_BOOTSTRAP_USER_ID
    ),
    updated_by_user_id: Optional[str] = (
        SYSTEM_BLOCK_BOOTSTRAP_USER_ID
    ),
) -> SystemBlockBootstrapResult:
    """
    Inspect and, when permitted, reconcile all built-in system blocks.

    The function never commits the outer transaction.

    Behaviour:

    1. Validate dependencies, catalog and registry.
    2. Build a complete read-only plan.
    3. Return immediately when already ready.
    4. Return the plan when dry-run is enabled.
    5. Return without mutation when any problem is not repairable.
    6. Apply all repairs inside one optional nested transaction.
    7. Flush when configured.
    8. Reinspect and verify final readiness.
    9. Roll back the nested transaction when final verification fails.
    """
    resolved_policy = (
        policy
        or get_default_system_block_bootstrap_policy()
    )

    resolved_created_by = (
        _normalize_optional_user_id(
            created_by_user_id
        )
    )

    resolved_updated_by = (
        _normalize_optional_user_id(
            updated_by_user_id
        )
    )

    resolved_registry = _require_usable_registry(
        registry,
        policy=resolved_policy,
    )

    require_system_block_catalog_ready()

    initial = inspect_system_blocks_for_registry(
        resolved_registry,
        policy=resolved_policy,
    )

    if initial.ready:
        return replace(
            initial,
            policy=resolved_policy,
            dry_run=resolved_policy.dry_run,
            nested_transaction_used=False,
        )

    if resolved_policy.dry_run:
        return replace(
            initial,
            policy=resolved_policy,
            dry_run=True,
            changed=False,
            nested_transaction_used=False,
            flushed=False,
            rolled_back=False,
        )

    if not initial.repairable:
        return replace(
            initial,
            policy=resolved_policy,
            dry_run=False,
            changed=False,
            nested_transaction_used=False,
            flushed=False,
            rolled_back=False,
        )

    mirror_actions: dict[
        str,
        tuple[str, bool, bool],
    ] = {}

    air_changed = False
    flushed = False
    nested_used = bool(
        resolved_policy.use_nested_transaction
    )

    try:
        transaction_context = (
            _build_transaction_context(
                resolved_policy
            )
        )

        with transaction_context:
            if not initial.air.ready:
                deleted_count = _apply_air_repair(
                    registry=resolved_registry,
                    policy=resolved_policy,
                )
                air_changed = deleted_count > 0

            for mirror_plan in initial.mirrors:
                if mirror_plan.ready:
                    mirror_actions[
                        mirror_plan.system_block_id
                    ] = (
                        ACTION_UNCHANGED,
                        False,
                        False,
                    )
                    continue

                (
                    _row,
                    action,
                    created,
                    updated,
                ) = _apply_mirror_plan(
                    registry=resolved_registry,
                    plan=mirror_plan,
                    policy=resolved_policy,
                    created_by_user_id=(
                        resolved_created_by
                    ),
                    updated_by_user_id=(
                        resolved_updated_by
                    ),
                )

                mirror_actions[
                    mirror_plan.system_block_id
                ] = (
                    action,
                    created,
                    updated,
                )

            if resolved_policy.flush:
                _flush_session()
                flushed = True

            verification_policy = (
                get_read_only_system_block_bootstrap_policy()
            )

            final_inspection = (
                inspect_system_blocks_for_registry(
                    resolved_registry,
                    policy=verification_policy,
                )
            )

            if not final_inspection.ready:
                raise _SystemBlockAtomicAbort(
                    "System-block verification failed after reconciliation."
                )

    except _SystemBlockAtomicAbort as exc:
        rollback_errors = list(
            initial.errors
        )
        rollback_errors.append(
            _safe_exception_text(exc)
        )

        rolled_back_mirrors = tuple(
            replace(
                mirror,
                ready=False,
                action=ACTION_ROLLED_BACK,
                changed=False,
                created=False,
                updated=False,
                errors=_normalize_error_messages(
                    (
                        *mirror.errors,
                        "Atomic system-block reconciliation "
                        "was rolled back.",
                    )
                ),
            )
            for mirror in initial.mirrors
        )

        return replace(
            initial,
            ready=False,
            policy=resolved_policy,
            dry_run=False,
            mirrors=rolled_back_mirrors,
            changed=False,
            nested_transaction_used=nested_used,
            flushed=flushed,
            rolled_back=nested_used,
            errors=_normalize_error_messages(
                rollback_errors
            ),
        )

    except Exception as exc:
        errors = list(initial.errors)
        errors.append(
            "System-block reconciliation failed: "
            f"{type(exc).__name__}: "
            f"{_safe_exception_text(exc)}"
        )

        failed_mirrors = tuple(
            replace(
                mirror,
                ready=False,
                action=(
                    ACTION_ROLLED_BACK
                    if nested_used
                    else ACTION_ERROR
                ),
                changed=False,
                created=False,
                updated=False,
            )
            for mirror in initial.mirrors
        )

        return replace(
            initial,
            ready=False,
            policy=resolved_policy,
            dry_run=False,
            mirrors=failed_mirrors,
            changed=False,
            nested_transaction_used=nested_used,
            flushed=flushed,
            rolled_back=nested_used,
            errors=_normalize_error_messages(
                errors
            ),
        )

    # Reinspection succeeded inside the transaction context.
    verification_policy = (
        get_read_only_system_block_bootstrap_policy()
    )

    final_inspection = (
        inspect_system_blocks_for_registry(
            resolved_registry,
            policy=verification_policy,
        )
    )

    initial_by_system_id = {
        mirror.system_block_id: mirror
        for mirror in initial.mirrors
    }

    combined_mirrors: list[
        SystemBlockMirrorResult
    ] = []

    for final_mirror in final_inspection.mirrors:
        plan = initial_by_system_id.get(
            final_mirror.system_block_id,
            final_mirror,
        )

        (
            action,
            created,
            updated,
        ) = mirror_actions.get(
            final_mirror.system_block_id,
            (
                ACTION_UNCHANGED,
                False,
                False,
            ),
        )

        combined_mirrors.append(
            _combine_final_mirror_result(
                plan=plan,
                final=final_mirror,
                action=action,
                created=created,
                updated=updated,
            )
        )

    final_air = replace(
        final_inspection.air,
        action=(
            ACTION_DELETED_ILLEGAL_AIR
            if air_changed
            else final_inspection.air.action
        ),
        would_change=(
            initial.air.would_change
        ),
        changed=air_changed,
    )

    changed = bool(
        air_changed
        or any(
            mirror.changed
            for mirror in combined_mirrors
        )
    )

    final_errors: list[Any] = []

    if not final_air.ready:
        final_errors.extend(
            final_air.errors
        )

    for mirror in combined_mirrors:
        if not mirror.ready:
            final_errors.extend(
                mirror.errors
            )

    final_ready = bool(
        final_air.ready
        and all(
            mirror.ready
            for mirror in combined_mirrors
        )
    )

    return SystemBlockBootstrapResult(
        ready=final_ready,
        repairable=final_ready,
        registry_db_id=(
            final_inspection.registry_db_id
        ),
        registry_id=(
            final_inspection.registry_id
        ),
        registry_version=(
            final_inspection.registry_version
        ),
        policy=resolved_policy,
        air=final_air,
        mirrors=tuple(combined_mirrors),
        dry_run=False,
        changed=changed,
        would_change=initial.would_change,
        nested_transaction_used=nested_used,
        flushed=flushed,
        rolled_back=False,
        errors=_normalize_error_messages(
            final_errors
        ),
    )


def ensure_system_blocks_for_registry(
    registry: Any,
    *,
    policy: Optional[
        SystemBlockBootstrapPolicy
    ] = None,
    created_by_user_id: Optional[str] = (
        SYSTEM_BLOCK_BOOTSTRAP_USER_ID
    ),
    updated_by_user_id: Optional[str] = (
        SYSTEM_BLOCK_BOOTSTRAP_USER_ID
    ),
) -> SystemBlockBootstrapResult:
    """
    Reconcile one registry and require a ready final state.

    The returned changes remain uncommitted until the caller commits the outer
    transaction.
    """
    result = reconcile_system_blocks_for_registry(
        registry,
        policy=policy,
        created_by_user_id=(
            created_by_user_id
        ),
        updated_by_user_id=(
            updated_by_user_id
        ),
    )

    result.require_ready()
    return result


def preview_system_block_bootstrap_for_registry(
    registry: Any,
    *,
    policy: Optional[
        SystemBlockBootstrapPolicy
    ] = None,
) -> SystemBlockBootstrapResult:
    """
    Return a dry-run reconciliation plan without database mutation.
    """
    base_policy = (
        policy
        or get_default_system_block_bootstrap_policy()
    )

    preview_policy = replace(
        base_policy,
        dry_run=True,
        use_nested_transaction=False,
        flush=False,
    )

    return inspect_system_blocks_for_registry(
        registry,
        policy=preview_policy,
    )


def build_system_block_bootstrap_status_for_registry(
    registry: Any,
) -> dict[str, Any]:
    """
    Return non-mutating JSON-safe readiness diagnostics.
    """
    try:
        result = inspect_system_blocks_for_registry(
            registry,
            policy=(
                get_read_only_system_block_bootstrap_policy()
            ),
        )

        return result.to_dict()

    except Exception as exc:
        return {
            "schemaVersion": (
                SYSTEM_BLOCK_BOOTSTRAP_SCHEMA_VERSION
            ),
            "ready": False,
            "source": SYSTEM_BLOCK_BOOTSTRAP_SOURCE,
            "moduleVersion": (
                SYSTEM_BLOCK_BOOTSTRAP_MODULE_VERSION
            ),
            "catalogId": SYSTEM_BLOCK_CATALOG_ID,
            "catalogVersion": (
                SYSTEM_BLOCK_CATALOG_VERSION
            ),
            "errors": [
                "Could not build system-block bootstrap status."
            ],
            "errorType": type(exc).__name__,
            "error": _safe_exception_text(exc),
        }


def require_air_invariant_for_registry(
    registry: Any,
) -> AirInvariantResult:
    """
    Require that the target registry contains no Air BlockType row.
    """
    resolved_registry = _require_usable_registry(
        registry,
        policy=(
            get_read_only_system_block_bootstrap_policy()
        ),
    )

    result = _inspect_air_invariant(
        registry=resolved_registry,
        policy=(
            get_read_only_system_block_bootstrap_policy()
        ),
    )

    if not result.ready:
        raise SystemBlockAirInvariantError(
            "; ".join(result.errors)
            or (
                "Air persistence invariant is not ready."
            )
        )

    return result


# -----------------------------------------------------------------------------
# Cache management
# -----------------------------------------------------------------------------

def clear_system_block_bootstrap_caches() -> None:
    """
    Clear only immutable/static bootstrap caches.

    Database rows and query results are never cached and therefore require no
    invalidation.
    """
    get_default_system_block_bootstrap_policy.cache_clear()
    get_read_only_system_block_bootstrap_policy.cache_clear()
    get_repair_system_block_bootstrap_policy.cache_clear()

    get_system_block_bootstrap_descriptor.cache_clear()

    _get_model_columns.cache_clear()
    _get_expected_persistent_values_cached.cache_clear()


# -----------------------------------------------------------------------------
# Public exports
# -----------------------------------------------------------------------------

__all__ = [
    "ACTION_CONFLICT",
    "ACTION_CREATED",
    "ACTION_DELETED_ILLEGAL_AIR",
    "ACTION_DRIFTED",
    "ACTION_ERROR",
    "ACTION_INVALID",
    "ACTION_MISSING",
    "ACTION_READY",
    "ACTION_ROLLED_BACK",
    "ACTION_SKIPPED",
    "ACTION_UNCHANGED",
    "ACTION_UPDATED",
    "ACTION_WOULD_CREATE",
    "ACTION_WOULD_DELETE_ILLEGAL_AIR",
    "ACTION_WOULD_UPDATE",
    "AIR_INVARIANT_RESULT_SCHEMA_VERSION",
    "SYSTEM_BLOCK_BOOTSTRAP_MODULE_VERSION",
    "SYSTEM_BLOCK_BOOTSTRAP_POLICY_SCHEMA_VERSION",
    "SYSTEM_BLOCK_BOOTSTRAP_SCHEMA_VERSION",
    "SYSTEM_BLOCK_BOOTSTRAP_SOURCE",
    "SYSTEM_BLOCK_BOOTSTRAP_USER_ID",
    "SYSTEM_BLOCK_MIRROR_RESULT_SCHEMA_VERSION",
    "AirInvariantResult",
    "SystemBlockAirInvariantError",
    "SystemBlockBootstrapDependencyError",
    "SystemBlockBootstrapError",
    "SystemBlockBootstrapNotReadyError",
    "SystemBlockBootstrapPolicy",
    "SystemBlockBootstrapResult",
    "SystemBlockDuplicateRowError",
    "SystemBlockMirrorError",
    "SystemBlockMirrorResult",
    "SystemBlockRegistryError",
    "build_system_block_bootstrap_status_for_registry",
    "clear_system_block_bootstrap_caches",
    "ensure_system_blocks_for_registry",
    "find_system_block_mirror_rows",
    "get_default_system_block_bootstrap_policy",
    "get_read_only_system_block_bootstrap_policy",
    "get_repair_system_block_bootstrap_policy",
    "get_system_block_bootstrap_descriptor",
    "inspect_system_blocks_for_registry",
    "preview_system_block_bootstrap_for_registry",
    "reconcile_system_blocks_for_registry",
    "require_air_invariant_for_registry",
]