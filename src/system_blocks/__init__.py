# services/vectoplan-chunk/src/system_blocks/__init__.py
"""
Public package facade for built-in VECTOPLAN system blocks.

This package owns immutable block definitions that are permanently integrated
into ``vectoplan-chunk``.

Built-in definitions:

- ``system_air``
  Reserved empty-cell state represented by ``cellValue = 0``. Air is never a
  persistent ``BlockType`` and never appears in a positive chunk palette.

- ``system_railing``
  Persistent built-in block mirrored into the ``BlockRegistry`` assigned to a
  concrete ``WorldInstance``.

The facade keeps child modules lazy. Importing ``src.system_blocks`` therefore
has no database, Flask, route-registration or bootstrap side effects.

Important ordering rule
-----------------------

All helper functions used by dataclass ``__post_init__`` methods are defined
before module-level specifications are instantiated. This prevents import-time
``NameError`` failures and is intentionally enforced by the structure of this
file.
"""

from __future__ import annotations

import importlib
import threading
import traceback
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from functools import lru_cache
from types import MappingProxyType, ModuleType
from typing import Any, Final, Optional, TYPE_CHECKING


# -----------------------------------------------------------------------------
# Static package metadata
# -----------------------------------------------------------------------------

SYSTEM_BLOCKS_PACKAGE_NAME: Final[str] = "src.system_blocks"
SYSTEM_BLOCKS_PACKAGE_VERSION: Final[str] = "1.0.1"

SYSTEM_BLOCKS_PACKAGE_SCHEMA_VERSION: Final[str] = (
    "system-blocks-package.schema.v1"
)

SYSTEM_BLOCKS_PACKAGE_STATUS_SCHEMA_VERSION: Final[str] = (
    "system-blocks-package-status.schema.v1"
)

SYSTEM_BLOCKS_PACKAGE_SOURCE_PATH: Final[str] = (
    "services/vectoplan-chunk/src/system_blocks"
)

SYSTEM_BLOCKS_PACKAGE_DESCRIPTION: Final[str] = (
    "Built-in immutable VECTOPLAN system-block definitions, catalog and "
    "explicit database reconciliation."
)

MODULE_CONTRACTS: Final[str] = "contracts"
MODULE_AIR: Final[str] = "air"
MODULE_RAILING: Final[str] = "railing"
MODULE_CATALOG: Final[str] = "catalog"
MODULE_BOOTSTRAP: Final[str] = "bootstrap"


# -----------------------------------------------------------------------------
# Primitive helpers
#
# These helpers must stay above SystemBlocksModuleSpec construction. The package
# creates specification instances at import time and their __post_init__ methods
# call these functions immediately.
# -----------------------------------------------------------------------------

def _safe_exception_text(error: BaseException | Any) -> str:
    """Return a robust exception message."""
    try:
        text = str(error).strip()
    except Exception:
        text = ""

    return text or type(error).__name__


def _normalize_required_text(
    value: Any,
    *,
    field_name: str,
) -> str:
    """Normalize required package declaration text."""
    if value is None:
        raise ValueError(f"{field_name} is required.")

    try:
        text = str(value).strip()
    except Exception as exc:
        raise ValueError(
            f"{field_name} must be text-like."
        ) from exc

    if not text:
        raise ValueError(f"{field_name} is required.")

    return text


def _normalize_optional_text(value: Any) -> Optional[str]:
    """Normalize optional package declaration text."""
    if value is None:
        return None

    try:
        text = str(value).strip()
    except Exception:
        return None

    return text or None


def _normalize_export_names(
    values: Sequence[Any],
) -> tuple[str, ...]:
    """Normalize and deduplicate declared export names."""
    if isinstance(values, (str, bytes, bytearray)):
        raw_values: Sequence[Any] = (values,)
    else:
        try:
            raw_values = tuple(values)
        except Exception as exc:
            raise ValueError(
                "exports must be a sequence."
            ) from exc

    result: list[str] = []
    seen: set[str] = set()

    for raw_value in raw_values:
        export_name = _normalize_required_text(
            raw_value,
            field_name="export_name",
        )

        if export_name in seen:
            continue

        seen.add(export_name)
        result.append(export_name)

    return tuple(result)


def _normalize_error_messages(
    errors: Sequence[Any] | Any | None,
) -> tuple[str, ...]:
    """Normalize and deduplicate errors while preserving order."""
    if errors is None:
        return tuple()

    if isinstance(errors, (str, bytes, bytearray)):
        raw_values = (errors,)
    else:
        try:
            raw_values = tuple(errors)
        except Exception:
            raw_values = (errors,)

    result: list[str] = []
    seen: set[str] = set()

    for raw_error in raw_values:
        try:
            text = str(raw_error).strip()
        except Exception:
            text = type(raw_error).__name__

        if not text or text in seen:
            continue

        seen.add(text)
        result.append(text)

    return tuple(result)


def _safe_module_name(module: Any) -> Optional[str]:
    """Return a module import name without raising."""
    try:
        return _normalize_optional_text(
            getattr(module, "__name__", None)
        )
    except Exception:
        return None


def _safe_module_path(module: Any) -> Optional[str]:
    """Return a module source path without raising."""
    try:
        return _normalize_optional_text(
            getattr(module, "__file__", None)
        )
    except Exception:
        return None


def _coerce_optional_bool(value: Any) -> Optional[bool]:
    """Convert common bool-like values while preserving unknown as None."""
    if value is None:
        return None

    if isinstance(value, bool):
        return value

    if isinstance(value, int):
        return bool(value)

    try:
        text = str(value).strip().lower()
    except Exception:
        return None

    if text in {
        "1",
        "true",
        "yes",
        "on",
        "enabled",
        "ready",
    }:
        return True

    if text in {
        "0",
        "false",
        "no",
        "off",
        "disabled",
        "not-ready",
    }:
        return False

    return None


def _make_json_safe(
    value: Any,
    *,
    _seen: Optional[set[int]] = None,
    _depth: int = 0,
    max_depth: int = 60,
) -> Any:
    """Convert diagnostics into recursion-safe JSON-compatible values."""
    if _depth > max_depth:
        return "<max-depth-exceeded>"

    if value is None or isinstance(value, (str, int, float, bool)):
        return value

    if _seen is None:
        _seen = set()

    if isinstance(value, Mapping):
        value_id = id(value)

        if value_id in _seen:
            return "<recursive-reference>"

        _seen.add(value_id)

        try:
            result: dict[str, Any] = {}

            for raw_key, raw_item in value.items():
                try:
                    key = str(raw_key)
                except Exception:
                    key = "<unserializable-key>"

                result[key] = _make_json_safe(
                    raw_item,
                    _seen=_seen,
                    _depth=_depth + 1,
                    max_depth=max_depth,
                )

            return result
        finally:
            _seen.discard(value_id)

    if isinstance(value, (list, tuple, set, frozenset)):
        value_id = id(value)

        if value_id in _seen:
            return "<recursive-reference>"

        _seen.add(value_id)

        try:
            return [
                _make_json_safe(
                    item,
                    _seen=_seen,
                    _depth=_depth + 1,
                    max_depth=max_depth,
                )
                for item in value
            ]
        finally:
            _seen.discard(value_id)

    to_dict = getattr(value, "to_dict", None)

    if callable(to_dict):
        try:
            return _make_json_safe(
                to_dict(),
                _seen=_seen,
                _depth=_depth + 1,
                max_depth=max_depth,
            )
        except Exception:
            pass

    try:
        return str(value)
    except Exception:
        return "<unserializable-value>"


def _extract_ready_value(
    value: Any,
) -> tuple[Optional[bool], Mapping[str, Any]]:
    """Resolve readiness from a bool, mapping or status-like object."""
    if isinstance(value, bool):
        return value, MappingProxyType({"ready": value})

    if isinstance(value, Mapping):
        details = dict(value)
        return (
            _coerce_optional_bool(details.get("ready")),
            MappingProxyType(details),
        )

    try:
        raw_ready = getattr(value, "ready")
    except Exception:
        raw_ready = None

    details: dict[str, Any] = {}
    to_dict = getattr(value, "to_dict", None)

    if callable(to_dict):
        try:
            raw_details = to_dict()
            if isinstance(raw_details, Mapping):
                details = dict(raw_details)
        except Exception:
            details = {}

    return (
        _coerce_optional_bool(raw_ready),
        MappingProxyType(details),
    )


# -----------------------------------------------------------------------------
# Exceptions
# -----------------------------------------------------------------------------

class SystemBlocksPackageError(RuntimeError):
    """Base error for the public system-block package facade."""


class SystemBlocksPackageImportError(SystemBlocksPackageError):
    """Raised when a required child module cannot be imported."""

    def __init__(
        self,
        module_key: str,
        *,
        attempted_paths: Sequence[str],
        import_errors: Mapping[str, str],
    ) -> None:
        self.module_key = str(module_key).strip()
        self.attempted_paths = tuple(
            str(path).strip()
            for path in attempted_paths
            if str(path).strip()
        )
        self.import_errors = MappingProxyType(
            {
                str(path): str(error)
                for path, error in import_errors.items()
            }
        )

        super().__init__(
            f"Could not import system-block module '{self.module_key}'. "
            f"Attempted paths: {', '.join(self.attempted_paths)}."
        )


class SystemBlocksPackageExportError(SystemBlocksPackageError):
    """Raised when a declared package export is missing."""

    def __init__(
        self,
        export_name: str,
        *,
        module_key: Optional[str] = None,
        message: Optional[str] = None,
    ) -> None:
        self.export_name = str(export_name).strip()
        self.module_key = (
            str(module_key).strip()
            if module_key is not None
            else None
        )

        super().__init__(
            message
            or (
                f"System-block package export '{self.export_name}' "
                "is unavailable"
                + (
                    f" from module '{self.module_key}'."
                    if self.module_key
                    else "."
                )
            )
        )


class SystemBlocksPackageValidationError(SystemBlocksPackageError):
    """Raised when module/export declarations are inconsistent."""

    def __init__(self, errors: Sequence[Any]) -> None:
        self.errors = _normalize_error_messages(errors)
        details = "; ".join(self.errors) or "unknown validation failure"
        super().__init__(
            f"Invalid system-block package configuration: {details}"
        )


class SystemBlocksPackageNotReadyError(SystemBlocksPackageError):
    """Raised when required package readiness checks fail."""

    def __init__(self, errors: Sequence[Any]) -> None:
        self.errors = _normalize_error_messages(errors)
        details = "; ".join(self.errors) or "unknown readiness failure"
        super().__init__(
            f"The built-in system-block package is not ready: {details}"
        )


# -----------------------------------------------------------------------------
# Data structures
# -----------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class SystemBlocksModuleSpec:
    """Declaration for one lazily loaded child module."""

    module_key: str
    package_segment: str
    exports: tuple[str, ...]
    order: int = 0
    required_for_package_readiness: bool = True
    readiness_factory_name: Optional[str] = None
    cache_clear_name: Optional[str] = None
    description: str = ""

    def __post_init__(self) -> None:
        try:
            module_key = _normalize_required_text(
                self.module_key,
                field_name="module_key",
            )
            package_segment = _normalize_required_text(
                self.package_segment,
                field_name="package_segment",
            )
            exports = _normalize_export_names(self.exports)
            order = int(self.order)
        except SystemBlocksPackageError:
            raise
        except Exception as exc:
            raise SystemBlocksPackageValidationError(
                (
                    f"Invalid module specification for "
                    f"'{getattr(self, 'module_key', '<unknown>')}': "
                    f"{type(exc).__name__}: {_safe_exception_text(exc)}",
                )
            ) from exc

        object.__setattr__(self, "module_key", module_key)
        object.__setattr__(self, "package_segment", package_segment)
        object.__setattr__(self, "exports", exports)
        object.__setattr__(self, "order", order)
        object.__setattr__(
            self,
            "required_for_package_readiness",
            bool(self.required_for_package_readiness),
        )
        object.__setattr__(
            self,
            "readiness_factory_name",
            _normalize_optional_text(self.readiness_factory_name),
        )
        object.__setattr__(
            self,
            "cache_clear_name",
            _normalize_optional_text(self.cache_clear_name),
        )
        object.__setattr__(
            self,
            "description",
            _normalize_optional_text(self.description) or "",
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "moduleKey": self.module_key,
            "packageSegment": self.package_segment,
            "exports": list(self.exports),
            "order": self.order,
            "requiredForPackageReadiness": (
                self.required_for_package_readiness
            ),
            "readinessFactoryName": self.readiness_factory_name,
            "cacheClearName": self.cache_clear_name,
            "description": self.description,
        }


@dataclass(frozen=True, slots=True)
class SystemBlocksModuleImportAttempt:
    """Result of one child-module import attempt."""

    module_key: str
    import_path: str
    imported: bool
    error_type: Optional[str] = None
    error: Optional[str] = None
    traceback_text: Optional[str] = field(default=None, repr=False)

    def to_dict(self, *, include_traceback: bool = False) -> dict[str, Any]:
        return {
            "moduleKey": self.module_key,
            "importPath": self.import_path,
            "imported": self.imported,
            "errorType": self.error_type,
            "error": self.error,
            "traceback": self.traceback_text if include_traceback else None,
        }


@dataclass(frozen=True, slots=True)
class SystemBlocksModuleStatus:
    """Import, export and readiness status for one child module."""

    module_key: str
    required: bool
    ready: bool
    imported: bool
    module_name: Optional[str]
    module_path: Optional[str]
    expected_exports: tuple[str, ...]
    available_exports: tuple[str, ...]
    missing_exports: tuple[str, ...]
    readiness_checked: bool
    readiness_value: Optional[bool]
    readiness_details: Mapping[str, Any]
    errors: tuple[str, ...]
    import_attempts: tuple[SystemBlocksModuleImportAttempt, ...]

    def to_dict(self, *, include_tracebacks: bool = False) -> dict[str, Any]:
        return {
            "moduleKey": self.module_key,
            "required": self.required,
            "ready": self.ready,
            "imported": self.imported,
            "moduleName": self.module_name,
            "modulePath": self.module_path,
            "expectedExports": list(self.expected_exports),
            "availableExports": list(self.available_exports),
            "missingExports": list(self.missing_exports),
            "readinessChecked": self.readiness_checked,
            "readinessValue": self.readiness_value,
            "readinessDetails": _make_json_safe(self.readiness_details),
            "errors": list(self.errors),
            "importAttempts": [
                attempt.to_dict(include_traceback=include_tracebacks)
                for attempt in self.import_attempts
            ],
        }


@dataclass(frozen=True, slots=True)
class SystemBlocksPackageStatus:
    """Complete public-package readiness status."""

    ready: bool
    package_name: str
    package_version: str
    source_path: str
    module_count: int
    required_module_count: int
    ready_module_count: int
    expected_export_count: int
    available_export_count: int
    missing_export_count: int
    module_statuses: tuple[SystemBlocksModuleStatus, ...]
    errors: tuple[str, ...]

    def to_dict(self, *, include_tracebacks: bool = False) -> dict[str, Any]:
        return {
            "schemaVersion": SYSTEM_BLOCKS_PACKAGE_STATUS_SCHEMA_VERSION,
            "ready": self.ready,
            "packageName": self.package_name,
            "packageVersion": self.package_version,
            "sourcePath": self.source_path,
            "description": SYSTEM_BLOCKS_PACKAGE_DESCRIPTION,
            "moduleCount": self.module_count,
            "requiredModuleCount": self.required_module_count,
            "readyModuleCount": self.ready_module_count,
            "expectedExportCount": self.expected_export_count,
            "availableExportCount": self.available_export_count,
            "missingExportCount": self.missing_export_count,
            "moduleStatuses": [
                status.to_dict(include_tracebacks=include_tracebacks)
                for status in self.module_statuses
            ],
            "errors": list(self.errors),
        }


# -----------------------------------------------------------------------------
# Curated child-module exports
# -----------------------------------------------------------------------------

CONTRACT_EXPORTS: Final[tuple[str, ...]] = (
    "AIR_CELL_VALUE",
    "BLOCK_CELL_VALUE_RULE",
    "CELL_ENCODING_VERSION",
    "DEFAULT_DEFINITION_VERSION",
    "RENDER_MODE_CUBE",
    "RENDER_MODE_CUSTOM",
    "RENDER_MODE_INVISIBLE",
    "RENDER_MODE_MESH",
    "SHAPE_TYPE_CUBE",
    "SHAPE_TYPE_CUSTOM",
    "SHAPE_TYPE_EMPTY",
    "SYSTEM_BLOCK_CATEGORY",
    "SYSTEM_BLOCK_DEFINITION_SCHEMA_VERSION",
    "SYSTEM_BLOCK_METADATA_NAMESPACE",
    "SYSTEM_BLOCK_METADATA_SCHEMA_VERSION",
    "SYSTEM_BLOCK_SOURCE",
    "SYSTEM_BLOCK_STATUS_ACTIVE",
    "SystemBlockContractError",
    "SystemBlockDefinition",
    "SystemBlockDefinitionValidationError",
    "SystemBlockPaletteError",
    "SystemBlockPersistenceError",
    "clear_system_block_contract_caches",
    "get_system_block_contract_descriptor",
    "make_json_safe",
    "require_system_block_definition",
    "serialize_system_block_definition",
    "validate_system_block_definition",
)

AIR_EXPORTS: Final[tuple[str, ...]] = (
    "AIR_CREATION_COMMAND",
    "AIR_DEFINITION_VERSION",
    "AIR_INVENTORY_VISIBLE",
    "AIR_KIND",
    "AIR_LABEL",
    "AIR_RENDER_MODE",
    "AIR_REPLACEABLE",
    "AIR_RESERVED_CELL_VALUE",
    "AIR_RUNTIME_BLOCK_TYPE_ID",
    "AIR_SET_BLOCK_ERROR_CODE",
    "AIR_SHAPE_TYPE",
    "AIR_SYSTEM_BLOCK_ID",
    "AIR_TARGETABLE",
    "AirDefinitionError",
    "AirInvariantError",
    "clear_air_definition_caches",
    "collect_air_invariant_errors",
    "get_air_definition",
    "get_air_definition_status",
    "get_air_metadata",
    "is_air_cell_value",
    "is_air_runtime_block_type_id",
    "is_air_system_block_id",
    "is_forbidden_air_set_block_id",
    "require_air_definition",
    "require_air_definition_ready",
    "serialize_air_definition",
    "serialize_air_for_world_blocks_route",
    "validate_air_definition",
)

RAILING_EXPORTS: Final[tuple[str, ...]] = (
    "RAILING_DEFINITION_VERSION",
    "RAILING_INVENTORY_VISIBLE",
    "RAILING_KIND",
    "RAILING_LABEL",
    "RAILING_PLACEMENT_COMMAND",
    "RAILING_REMOVAL_COMMAND",
    "RAILING_RENDER_MODE",
    "RAILING_REPLACEABLE",
    "RAILING_RUNTIME_BLOCK_TYPE_ID",
    "RAILING_SHAPE_TYPE",
    "RAILING_SYSTEM_BLOCK_ID",
    "RAILING_TARGETABLE",
    "RailingDefinitionError",
    "RailingInvariantError",
    "RailingSerializationError",
    "build_railing_palette_entry",
    "build_railing_persistent_values",
    "clear_railing_definition_caches",
    "collect_railing_invariant_errors",
    "compare_railing_block_type",
    "get_railing_definition",
    "get_railing_definition_debug_summary",
    "get_railing_definition_status",
    "get_railing_metadata",
    "is_railing_block_type_in_sync",
    "is_railing_identifier",
    "is_railing_runtime_block_type_id",
    "is_railing_system_block_id",
    "require_railing_definition",
    "require_railing_definition_ready",
    "serialize_railing_definition",
    "serialize_railing_for_system_catalog",
    "validate_railing_definition",
)

CATALOG_EXPORTS: Final[tuple[str, ...]] = (
    "SYSTEM_BLOCK_CATALOG_ID",
    "SYSTEM_BLOCK_CATALOG_MODULE_VERSION",
    "SYSTEM_BLOCK_CATALOG_SCHEMA_VERSION",
    "SYSTEM_BLOCK_CATALOG_STATUS_SCHEMA_VERSION",
    "SYSTEM_BLOCK_CATALOG_VERSION",
    "SYSTEM_BLOCK_ID_PREFIX",
    "SystemBlockCatalog",
    "SystemBlockCatalogError",
    "SystemBlockCatalogNotReadyError",
    "SystemBlockCatalogStatus",
    "SystemBlockCatalogValidationError",
    "SystemBlockLookupError",
    "SystemBlockProviderError",
    "SystemBlockProviderStatus",
    "canonical_runtime_block_type_id",
    "clear_system_block_catalog_caches",
    "collect_system_block_catalog_errors",
    "get_air_system_block_definition",
    "get_inventory_system_block_definitions",
    "get_persistent_system_block_definitions",
    "get_persistent_system_block_values_by_runtime_id",
    "get_railing_system_block_definition",
    "get_reserved_system_block_definition_for_cell_value",
    "get_reserved_system_block_definitions",
    "get_reserved_system_identifiers",
    "get_system_block_catalog",
    "get_system_block_catalog_debug_summary",
    "get_system_block_catalog_descriptor",
    "get_system_block_catalog_status",
    "get_system_block_definition",
    "get_system_block_definitions",
    "is_reserved_cell_state_identifier",
    "is_reserved_system_identifier",
    "is_system_block_catalog_ready",
    "is_system_block_identifier",
    "is_system_runtime_block_type_id",
    "require_system_block_catalog_ready",
    "require_system_block_definition_from_catalog",
    "requires_remove_block_for_identifier",
    "serialize_system_block_catalog",
    "serialize_system_block_definition_from_catalog",
)

BOOTSTRAP_EXPORTS: Final[tuple[str, ...]] = (
    "SYSTEM_BLOCK_BOOTSTRAP_MODULE_VERSION",
    "SYSTEM_BLOCK_BOOTSTRAP_SCHEMA_VERSION",
    "SYSTEM_BLOCK_BOOTSTRAP_SOURCE",
    "SYSTEM_BLOCK_BOOTSTRAP_USER_ID",
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
)


# -----------------------------------------------------------------------------
# Module specifications
#
# Helper functions are already defined above, so dataclass normalization is safe
# during module import.
# -----------------------------------------------------------------------------

SYSTEM_BLOCKS_MODULE_SPECS: Final[
    tuple[SystemBlocksModuleSpec, ...]
] = (
    SystemBlocksModuleSpec(
        module_key=MODULE_CONTRACTS,
        package_segment=MODULE_CONTRACTS,
        exports=CONTRACT_EXPORTS,
        order=0,
        required_for_package_readiness=True,
        readiness_factory_name=None,
        cache_clear_name="clear_system_block_contract_caches",
        description="Shared immutable system-block contract and validation.",
    ),
    SystemBlocksModuleSpec(
        module_key=MODULE_AIR,
        package_segment=MODULE_AIR,
        exports=AIR_EXPORTS,
        order=100,
        required_for_package_readiness=True,
        readiness_factory_name="get_air_definition_status",
        cache_clear_name="clear_air_package_caches",
        description="Canonical reserved Air definition.",
    ),
    SystemBlocksModuleSpec(
        module_key=MODULE_RAILING,
        package_segment=MODULE_RAILING,
        exports=RAILING_EXPORTS,
        order=200,
        required_for_package_readiness=True,
        readiness_factory_name="get_railing_definition_status",
        cache_clear_name="clear_railing_package_caches",
        description="Canonical persistent Railing definition.",
    ),
    SystemBlocksModuleSpec(
        module_key=MODULE_CATALOG,
        package_segment=MODULE_CATALOG,
        exports=CATALOG_EXPORTS,
        order=300,
        required_for_package_readiness=True,
        readiness_factory_name="get_system_block_catalog_status",
        cache_clear_name="clear_system_block_catalog_caches",
        description="Validated immutable system-block catalog and lookup API.",
    ),
    SystemBlocksModuleSpec(
        module_key=MODULE_BOOTSTRAP,
        package_segment=MODULE_BOOTSTRAP,
        exports=BOOTSTRAP_EXPORTS,
        order=400,
        required_for_package_readiness=True,
        readiness_factory_name=None,
        cache_clear_name="clear_system_block_bootstrap_caches",
        description=(
            "Explicit database reconciliation. Registry-specific database "
            "readiness is not evaluated at package level."
        ),
    ),
)


if TYPE_CHECKING:
    from .air import get_air_definition, get_air_definition_status
    from .bootstrap import (
        SystemBlockBootstrapPolicy,
        SystemBlockBootstrapResult,
        ensure_system_blocks_for_registry,
    )
    from .catalog import (
        SystemBlockCatalog,
        get_system_block_catalog,
        get_system_block_catalog_status,
    )
    from .contracts import SystemBlockDefinition
    from .railing import get_railing_definition, get_railing_definition_status


# -----------------------------------------------------------------------------
# Internal import-attempt state
# -----------------------------------------------------------------------------

_IMPORT_ATTEMPTS_LOCK = threading.RLock()
_MODULE_IMPORT_ATTEMPTS: dict[
    str,
    list[SystemBlocksModuleImportAttempt],
] = {}


# -----------------------------------------------------------------------------
# Module-spec indexes
# -----------------------------------------------------------------------------

@lru_cache(maxsize=1)
def get_system_blocks_module_specs() -> tuple[SystemBlocksModuleSpec, ...]:
    """Return child-module specifications in deterministic order."""
    return tuple(
        sorted(
            SYSTEM_BLOCKS_MODULE_SPECS,
            key=lambda spec: (spec.order, spec.module_key),
        )
    )


@lru_cache(maxsize=1)
def get_system_blocks_module_spec_map() -> Mapping[str, SystemBlocksModuleSpec]:
    """Return immutable module-key index."""
    result: dict[str, SystemBlocksModuleSpec] = {}
    errors: list[str] = []

    for spec in get_system_blocks_module_specs():
        key = spec.module_key.casefold()

        if key in result:
            errors.append(f"Duplicate module key '{spec.module_key}'.")
            continue

        result[key] = spec

    if errors:
        raise SystemBlocksPackageValidationError(errors)

    return MappingProxyType(result)


@lru_cache(maxsize=1)
def get_system_blocks_export_map() -> Mapping[str, str]:
    """Return immutable export-name-to-module-key mapping."""
    result: dict[str, str] = {}
    errors: list[str] = []

    for spec in get_system_blocks_module_specs():
        for export_name in spec.exports:
            previous = result.get(export_name)

            if previous is not None and previous != spec.module_key:
                errors.append(
                    f"Export '{export_name}' is declared by both "
                    f"'{previous}' and '{spec.module_key}'."
                )
                continue

            result[export_name] = spec.module_key

    if errors:
        raise SystemBlocksPackageValidationError(errors)

    return MappingProxyType(result)


def get_system_blocks_module_spec(
    module_key: Any,
) -> Optional[SystemBlocksModuleSpec]:
    """Resolve a child-module specification."""
    normalized = _normalize_optional_text(module_key)

    if normalized is None:
        return None

    return get_system_blocks_module_spec_map().get(normalized.casefold())


def require_system_blocks_module_spec(
    module_key: Any,
) -> SystemBlocksModuleSpec:
    """Resolve a child-module specification or raise."""
    spec = get_system_blocks_module_spec(module_key)

    if spec is None:
        raise SystemBlocksPackageValidationError(
            (f"Unknown system-block module '{module_key}'.",)
        )

    return spec


# -----------------------------------------------------------------------------
# Import-attempt handling
# -----------------------------------------------------------------------------

def _record_module_import_attempt(
    attempt: SystemBlocksModuleImportAttempt,
) -> None:
    try:
        with _IMPORT_ATTEMPTS_LOCK:
            _MODULE_IMPORT_ATTEMPTS.setdefault(
                attempt.module_key,
                [],
            ).append(attempt)
    except Exception:
        pass


def _get_module_import_attempts(
    module_key: str,
) -> tuple[SystemBlocksModuleImportAttempt, ...]:
    try:
        with _IMPORT_ATTEMPTS_LOCK:
            return tuple(_MODULE_IMPORT_ATTEMPTS.get(module_key, ()))
    except Exception:
        return tuple()


# -----------------------------------------------------------------------------
# Lazy child-module import
# -----------------------------------------------------------------------------

@lru_cache(maxsize=64)
def get_system_blocks_module_import_paths(
    module_key: str,
) -> tuple[str, ...]:
    """Return supported import paths for one child module."""
    spec = require_system_blocks_module_spec(module_key)
    candidates: list[str] = []

    current_package = _normalize_optional_text(__name__)

    if current_package:
        candidates.append(f"{current_package}.{spec.package_segment}")

    candidates.extend(
        (
            f"src.system_blocks.{spec.package_segment}",
            f"system_blocks.{spec.package_segment}",
        )
    )

    result: list[str] = []
    seen: set[str] = set()

    for candidate in candidates:
        normalized = _normalize_optional_text(candidate)

        if normalized is None or normalized in seen:
            continue

        seen.add(normalized)
        result.append(normalized)

    return tuple(result)


@lru_cache(maxsize=64)
def get_system_blocks_module(module_key: str) -> ModuleType:
    """Import and cache one child module."""
    spec = require_system_blocks_module_spec(module_key)
    attempted_paths = get_system_blocks_module_import_paths(spec.module_key)
    import_errors: dict[str, str] = {}

    for import_path in attempted_paths:
        try:
            module = importlib.import_module(import_path)
        except Exception as exc:
            error_text = _safe_exception_text(exc)
            import_errors[import_path] = f"{type(exc).__name__}: {error_text}"
            _record_module_import_attempt(
                SystemBlocksModuleImportAttempt(
                    module_key=spec.module_key,
                    import_path=import_path,
                    imported=False,
                    error_type=type(exc).__name__,
                    error=error_text,
                    traceback_text=traceback.format_exc(),
                )
            )
            continue

        if not isinstance(module, ModuleType):
            error_text = "importlib returned an object that is not a ModuleType"
            import_errors[import_path] = f"TypeError: {error_text}"
            _record_module_import_attempt(
                SystemBlocksModuleImportAttempt(
                    module_key=spec.module_key,
                    import_path=import_path,
                    imported=False,
                    error_type="TypeError",
                    error=error_text,
                )
            )
            continue

        _record_module_import_attempt(
            SystemBlocksModuleImportAttempt(
                module_key=spec.module_key,
                import_path=import_path,
                imported=True,
            )
        )
        return module

    raise SystemBlocksPackageImportError(
        spec.module_key,
        attempted_paths=attempted_paths,
        import_errors=import_errors,
    )


# -----------------------------------------------------------------------------
# Lazy export resolution
# -----------------------------------------------------------------------------

@lru_cache(maxsize=1024)
def _resolve_system_blocks_export_cached(export_name: str) -> Any:
    normalized_name = _normalize_required_text(
        export_name,
        field_name="export_name",
    )

    module_key = get_system_blocks_export_map().get(normalized_name)

    if module_key is None:
        raise AttributeError(
            f"Module '{__name__}' has no public attribute '{normalized_name}'."
        )

    module = get_system_blocks_module(module_key)

    try:
        return getattr(module, normalized_name)
    except AttributeError as exc:
        raise SystemBlocksPackageExportError(
            normalized_name,
            module_key=module_key,
        ) from exc
    except Exception as exc:
        raise SystemBlocksPackageExportError(
            normalized_name,
            module_key=module_key,
            message=(
                f"Could not resolve package export '{normalized_name}' "
                f"from module '{module_key}': "
                f"{type(exc).__name__}: {_safe_exception_text(exc)}"
            ),
        ) from exc


def get_system_blocks_export(export_name: Any) -> Any:
    normalized = _normalize_required_text(
        export_name,
        field_name="export_name",
    )
    return _resolve_system_blocks_export_cached(normalized)


@lru_cache(maxsize=1)
def get_system_blocks_exports() -> Mapping[str, Any]:
    """Resolve all declared facade exports."""
    return MappingProxyType(
        {
            export_name: get_system_blocks_export(export_name)
            for export_name in get_system_blocks_export_map()
        }
    )


# -----------------------------------------------------------------------------
# Readiness
# -----------------------------------------------------------------------------

def _get_available_module_exports(
    module: ModuleType,
    spec: SystemBlocksModuleSpec,
) -> tuple[str, ...]:
    result: list[str] = []

    for export_name in spec.exports:
        try:
            getattr(module, export_name)
        except Exception:
            continue
        result.append(export_name)

    return tuple(sorted(result))


def _read_module_readiness(
    module: ModuleType,
    spec: SystemBlocksModuleSpec,
) -> tuple[bool, Optional[bool], Mapping[str, Any], tuple[str, ...]]:
    factory_name = spec.readiness_factory_name

    if factory_name is None:
        return False, None, MappingProxyType({}), tuple()

    try:
        factory = getattr(module, factory_name)
    except Exception as exc:
        return (
            True,
            False,
            MappingProxyType({}),
            (
                f"Readiness export '{factory_name}' is unavailable: "
                f"{type(exc).__name__}: {_safe_exception_text(exc)}",
            ),
        )

    if not callable(factory):
        return (
            True,
            False,
            MappingProxyType({}),
            (f"Readiness export '{factory_name}' is not callable.",),
        )

    try:
        raw_status = factory()
    except Exception as exc:
        return (
            True,
            False,
            MappingProxyType({}),
            (
                f"Calling readiness export '{factory_name}' failed: "
                f"{type(exc).__name__}: {_safe_exception_text(exc)}",
            ),
        )

    ready_value, details = _extract_ready_value(raw_status)
    errors: list[Any] = []

    if ready_value is None:
        errors.append(
            f"Readiness export '{factory_name}' did not provide "
            "a boolean ready value."
        )
    elif ready_value is False:
        raw_errors = details.get("errors") if isinstance(details, Mapping) else None

        if isinstance(raw_errors, (list, tuple, set, frozenset)):
            errors.extend(raw_errors)
        elif raw_errors:
            errors.append(raw_errors)

        raw_error = details.get("error") if isinstance(details, Mapping) else None
        if raw_error:
            errors.append(raw_error)

        if not errors:
            errors.append(
                f"Readiness export '{factory_name}' reports ready=false."
            )

    return (
        True,
        ready_value,
        details,
        _normalize_error_messages(errors),
    )


@lru_cache(maxsize=64)
def get_system_blocks_module_status(
    module_key: str,
) -> SystemBlocksModuleStatus:
    spec = require_system_blocks_module_spec(module_key)
    errors: list[Any] = []

    try:
        module = get_system_blocks_module(spec.module_key)
    except Exception as exc:
        errors.append(
            f"Module import failed: {type(exc).__name__}: "
            f"{_safe_exception_text(exc)}"
        )
        return SystemBlocksModuleStatus(
            module_key=spec.module_key,
            required=spec.required_for_package_readiness,
            ready=False,
            imported=False,
            module_name=None,
            module_path=None,
            expected_exports=spec.exports,
            available_exports=tuple(),
            missing_exports=spec.exports,
            readiness_checked=False,
            readiness_value=None,
            readiness_details=MappingProxyType({}),
            errors=_normalize_error_messages(errors),
            import_attempts=_get_module_import_attempts(spec.module_key),
        )

    available_exports = _get_available_module_exports(module, spec)
    available_set = set(available_exports)
    missing_exports = tuple(
        export_name
        for export_name in spec.exports
        if export_name not in available_set
    )

    if missing_exports:
        errors.append(
            f"Module '{spec.module_key}' is missing exports: "
            + ", ".join(missing_exports)
        )

    (
        readiness_checked,
        readiness_value,
        readiness_details,
        readiness_errors,
    ) = _read_module_readiness(module, spec)

    errors.extend(readiness_errors)
    readiness_ok = not readiness_checked or readiness_value is True
    normalized_errors = _normalize_error_messages(errors)

    return SystemBlocksModuleStatus(
        module_key=spec.module_key,
        required=spec.required_for_package_readiness,
        ready=bool(
            not missing_exports
            and readiness_ok
            and not normalized_errors
        ),
        imported=True,
        module_name=_safe_module_name(module),
        module_path=_safe_module_path(module),
        expected_exports=spec.exports,
        available_exports=available_exports,
        missing_exports=missing_exports,
        readiness_checked=readiness_checked,
        readiness_value=readiness_value,
        readiness_details=readiness_details,
        errors=normalized_errors,
        import_attempts=_get_module_import_attempts(spec.module_key),
    )


@lru_cache(maxsize=1)
def get_system_blocks_package_status() -> SystemBlocksPackageStatus:
    module_statuses: list[SystemBlocksModuleStatus] = []
    errors: list[Any] = []

    for spec in get_system_blocks_module_specs():
        try:
            status = get_system_blocks_module_status(spec.module_key)
        except Exception as exc:
            status = SystemBlocksModuleStatus(
                module_key=spec.module_key,
                required=spec.required_for_package_readiness,
                ready=False,
                imported=False,
                module_name=None,
                module_path=None,
                expected_exports=spec.exports,
                available_exports=tuple(),
                missing_exports=spec.exports,
                readiness_checked=False,
                readiness_value=None,
                readiness_details=MappingProxyType({}),
                errors=(
                    f"Could not build module status: "
                    f"{type(exc).__name__}: {_safe_exception_text(exc)}",
                ),
                import_attempts=_get_module_import_attempts(spec.module_key),
            )

        module_statuses.append(status)

        if status.required and not status.ready:
            errors.append(
                f"Required module '{status.module_key}' is not ready."
            )
            errors.extend(status.errors)

    expected_export_count = sum(
        len(spec.exports)
        for spec in get_system_blocks_module_specs()
    )
    available_export_count = sum(
        len(status.available_exports)
        for status in module_statuses
    )
    missing_export_count = sum(
        len(status.missing_exports)
        for status in module_statuses
    )
    required_module_count = sum(
        1
        for spec in get_system_blocks_module_specs()
        if spec.required_for_package_readiness
    )
    ready_module_count = sum(
        1 for status in module_statuses if status.ready
    )
    normalized_errors = _normalize_error_messages(errors)
    required_modules_ready = all(
        status.ready
        for status in module_statuses
        if status.required
    )

    return SystemBlocksPackageStatus(
        ready=bool(
            required_modules_ready
            and missing_export_count == 0
            and not normalized_errors
        ),
        package_name=SYSTEM_BLOCKS_PACKAGE_NAME,
        package_version=SYSTEM_BLOCKS_PACKAGE_VERSION,
        source_path=SYSTEM_BLOCKS_PACKAGE_SOURCE_PATH,
        module_count=len(module_statuses),
        required_module_count=required_module_count,
        ready_module_count=ready_module_count,
        expected_export_count=expected_export_count,
        available_export_count=available_export_count,
        missing_export_count=missing_export_count,
        module_statuses=tuple(module_statuses),
        errors=normalized_errors,
    )


def is_system_blocks_package_ready() -> bool:
    try:
        return bool(get_system_blocks_package_status().ready)
    except Exception:
        return False


def require_system_blocks_package_ready() -> SystemBlocksPackageStatus:
    status = get_system_blocks_package_status()

    if not status.ready:
        raise SystemBlocksPackageNotReadyError(
            status.errors or ("System-block package readiness check failed.",)
        )

    return status


def get_system_blocks_package_debug_summary(
    *,
    include_tracebacks: bool = False,
) -> dict[str, Any]:
    try:
        return get_system_blocks_package_status().to_dict(
            include_tracebacks=include_tracebacks
        )
    except Exception as exc:
        return {
            "schemaVersion": SYSTEM_BLOCKS_PACKAGE_STATUS_SCHEMA_VERSION,
            "ready": False,
            "packageName": SYSTEM_BLOCKS_PACKAGE_NAME,
            "packageVersion": SYSTEM_BLOCKS_PACKAGE_VERSION,
            "sourcePath": SYSTEM_BLOCKS_PACKAGE_SOURCE_PATH,
            "errors": [
                "Could not build system-block package diagnostics."
            ],
            "errorType": type(exc).__name__,
            "error": _safe_exception_text(exc),
        }


@lru_cache(maxsize=1)
def get_system_blocks_package_descriptor() -> Mapping[str, Any]:
    return MappingProxyType(
        {
            "schemaVersion": SYSTEM_BLOCKS_PACKAGE_SCHEMA_VERSION,
            "statusSchemaVersion": (
                SYSTEM_BLOCKS_PACKAGE_STATUS_SCHEMA_VERSION
            ),
            "packageName": SYSTEM_BLOCKS_PACKAGE_NAME,
            "packageVersion": SYSTEM_BLOCKS_PACKAGE_VERSION,
            "sourcePath": SYSTEM_BLOCKS_PACKAGE_SOURCE_PATH,
            "description": SYSTEM_BLOCKS_PACKAGE_DESCRIPTION,
            "lazyImports": True,
            "importSideEffects": False,
            "createsDatabaseRowsOnImport": False,
            "commitsTransactions": False,
            "moduleKeys": tuple(
                spec.module_key
                for spec in get_system_blocks_module_specs()
            ),
            "exportCount": len(get_system_blocks_export_map()),
        }
    )


# -----------------------------------------------------------------------------
# Lazy package attribute protocol
# -----------------------------------------------------------------------------

def __getattr__(name: str) -> Any:
    export_map = get_system_blocks_export_map()

    if name in export_map:
        value = get_system_blocks_export(name)

        try:
            globals()[name] = value
        except Exception:
            pass

        return value

    raise AttributeError(
        f"Module '{__name__}' has no attribute '{name}'."
    )


def __dir__() -> list[str]:
    names = set(globals().keys())

    try:
        names.update(get_system_blocks_export_map().keys())
    except Exception:
        pass

    return sorted(names)


# -----------------------------------------------------------------------------
# Cache management
# -----------------------------------------------------------------------------

def clear_system_blocks_package_caches(
    *,
    clear_child_module_caches: bool = True,
    clear_import_attempts: bool = True,
) -> None:
    loaded_modules: list[tuple[SystemBlocksModuleSpec, ModuleType]] = []

    if clear_child_module_caches:
        for spec in get_system_blocks_module_specs():
            try:
                module = get_system_blocks_module(spec.module_key)
            except Exception:
                continue

            loaded_modules.append((spec, module))

        for spec, module in reversed(loaded_modules):
            if not spec.cache_clear_name:
                continue

            try:
                clear_function = getattr(
                    module,
                    spec.cache_clear_name,
                    None,
                )
                if callable(clear_function):
                    clear_function()
            except Exception:
                pass

    try:
        export_names = tuple(get_system_blocks_export_map().keys())
    except Exception:
        export_names = tuple()

    for export_name in export_names:
        try:
            globals().pop(export_name, None)
        except Exception:
            pass

    _resolve_system_blocks_export_cached.cache_clear()
    get_system_blocks_exports.cache_clear()
    get_system_blocks_module_status.cache_clear()
    get_system_blocks_package_status.cache_clear()
    get_system_blocks_package_descriptor.cache_clear()
    get_system_blocks_module.cache_clear()
    get_system_blocks_module_import_paths.cache_clear()
    get_system_blocks_export_map.cache_clear()
    get_system_blocks_module_spec_map.cache_clear()
    get_system_blocks_module_specs.cache_clear()

    if clear_import_attempts:
        try:
            with _IMPORT_ATTEMPTS_LOCK:
                _MODULE_IMPORT_ATTEMPTS.clear()
        except Exception:
            pass


# -----------------------------------------------------------------------------
# Public exports
# -----------------------------------------------------------------------------

_PACKAGE_LOCAL_EXPORTS: Final[tuple[str, ...]] = (
    "AIR_EXPORTS",
    "BOOTSTRAP_EXPORTS",
    "CATALOG_EXPORTS",
    "CONTRACT_EXPORTS",
    "MODULE_AIR",
    "MODULE_BOOTSTRAP",
    "MODULE_CATALOG",
    "MODULE_CONTRACTS",
    "MODULE_RAILING",
    "RAILING_EXPORTS",
    "SYSTEM_BLOCKS_MODULE_SPECS",
    "SYSTEM_BLOCKS_PACKAGE_DESCRIPTION",
    "SYSTEM_BLOCKS_PACKAGE_NAME",
    "SYSTEM_BLOCKS_PACKAGE_SCHEMA_VERSION",
    "SYSTEM_BLOCKS_PACKAGE_SOURCE_PATH",
    "SYSTEM_BLOCKS_PACKAGE_STATUS_SCHEMA_VERSION",
    "SYSTEM_BLOCKS_PACKAGE_VERSION",
    "SystemBlocksModuleImportAttempt",
    "SystemBlocksModuleSpec",
    "SystemBlocksModuleStatus",
    "SystemBlocksPackageError",
    "SystemBlocksPackageExportError",
    "SystemBlocksPackageImportError",
    "SystemBlocksPackageNotReadyError",
    "SystemBlocksPackageStatus",
    "SystemBlocksPackageValidationError",
    "clear_system_blocks_package_caches",
    "get_system_blocks_export",
    "get_system_blocks_exports",
    "get_system_blocks_export_map",
    "get_system_blocks_module",
    "get_system_blocks_module_import_paths",
    "get_system_blocks_module_spec",
    "get_system_blocks_module_spec_map",
    "get_system_blocks_module_specs",
    "get_system_blocks_module_status",
    "get_system_blocks_package_debug_summary",
    "get_system_blocks_package_descriptor",
    "get_system_blocks_package_status",
    "is_system_blocks_package_ready",
    "require_system_blocks_module_spec",
    "require_system_blocks_package_ready",
)

try:
    _DELEGATED_PUBLIC_EXPORTS = tuple(
        get_system_blocks_export_map().keys()
    )
except Exception:
    _DELEGATED_PUBLIC_EXPORTS = tuple()

__all__ = [
    *_PACKAGE_LOCAL_EXPORTS,
    *_DELEGATED_PUBLIC_EXPORTS,
]
