# services/vectoplan-chunk/src/system_blocks/catalog.py
"""
Immutable catalog for built-in VECTOPLAN system blocks.

The catalog is the framework-independent aggregation layer between the
individual code-owned definitions and the database bootstrap integration.

Current built-in definitions:

``system_air``
    Reserved empty-cell state. Air owns ``cellValue = 0``, is not persisted as
    a ``BlockType`` and must be created through ``RemoveBlock`` rather than
    ``SetBlock``.

``system_railing``
    Persistent built-in block. Railing is mirrored into each runtime
    ``BlockRegistry`` and receives a positive chunk-local cell value through the
    normal palette rule ``paletteIndex + 1``.

Import-order rule
-----------------

All helpers used by dataclass ``__post_init__`` methods are defined before the
module-level provider specifications are instantiated. This is intentional and
prevents import-time ``NameError`` failures.

Cache rule
----------

Only immutable provider modules, definitions, catalog indexes, descriptors and
serialized definition data are cached. Database rows and SQLAlchemy objects are
never imported or cached here.
"""

from __future__ import annotations

import importlib
import threading
import traceback
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from functools import lru_cache
from types import MappingProxyType, ModuleType
from typing import Any, Final, Optional, TYPE_CHECKING


# -----------------------------------------------------------------------------
# Contract imports
# -----------------------------------------------------------------------------

try:
    from .contracts import (
        AIR_CELL_VALUE,
        SystemBlockDefinition,
        make_json_safe,
        require_system_block_definition,
    )
except ImportError:
    try:
        from src.system_blocks.contracts import (
            AIR_CELL_VALUE,
            SystemBlockDefinition,
            make_json_safe,
            require_system_block_definition,
        )
    except ImportError as exc:
        raise RuntimeError(
            "Could not import system-block contracts while loading the "
            "built-in catalog."
        ) from exc


if TYPE_CHECKING:
    from collections.abc import Callable


# -----------------------------------------------------------------------------
# Static catalog metadata
# -----------------------------------------------------------------------------

SYSTEM_BLOCK_CATALOG_ID: Final[str] = "vectoplan-built-in-system-blocks"
SYSTEM_BLOCK_CATALOG_VERSION: Final[str] = "1"
SYSTEM_BLOCK_CATALOG_MODULE_VERSION: Final[str] = "1.0.1"

SYSTEM_BLOCK_CATALOG_SCHEMA_VERSION: Final[str] = (
    "system-block-catalog.schema.v1"
)

SYSTEM_BLOCK_CATALOG_STATUS_SCHEMA_VERSION: Final[str] = (
    "system-block-catalog-status.schema.v1"
)

SYSTEM_BLOCK_PROVIDER_STATUS_SCHEMA_VERSION: Final[str] = (
    "system-block-provider-status.schema.v1"
)

SYSTEM_BLOCK_ID_PREFIX: Final[str] = "system_"

AIR_PROVIDER_KEY: Final[str] = "air"
RAILING_PROVIDER_KEY: Final[str] = "railing"

AIR_SYSTEM_BLOCK_ID: Final[str] = "system_air"
RAILING_SYSTEM_BLOCK_ID: Final[str] = "system_railing"
RAILING_RUNTIME_BLOCK_TYPE_ID: Final[str] = "system_railing"


# -----------------------------------------------------------------------------
# Primitive helpers
#
# These functions deliberately appear before every dataclass instance created at
# module scope. Provider-spec __post_init__ methods call them during import.
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
    """Normalize required declaration text."""
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
    """Normalize optional text."""
    if value is None:
        return None

    try:
        text = str(value).strip()
    except Exception:
        return None

    return text or None


def _normalize_lookup_text(value: Any) -> Optional[str]:
    """Normalize a case-insensitive catalog lookup key."""
    text = _normalize_optional_text(value)

    if text is None:
        return None

    return text.casefold()


def _normalize_string_tuple(
    values: Iterable[Any] | Any | None,
) -> tuple[str, ...]:
    """Normalize and deduplicate a sequence of non-empty strings."""
    if values is None:
        return tuple()

    if isinstance(values, (str, bytes, bytearray)):
        raw_values = (values,)
    else:
        try:
            raw_values = tuple(values)
        except Exception:
            raw_values = (values,)

    result: list[str] = []
    seen: set[str] = set()

    for raw_value in raw_values:
        text = _normalize_optional_text(raw_value)

        if text is None:
            continue

        lookup = text.casefold()

        if lookup in seen:
            continue

        seen.add(lookup)
        result.append(text)

    return tuple(result)


def _normalize_error_messages(
    errors: Iterable[Any] | Any | None,
) -> tuple[str, ...]:
    """Normalize and deduplicate diagnostic messages."""
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


def _safe_bool(value: Any, default: bool = False) -> bool:
    """Normalize common boolean-like values."""
    if isinstance(value, bool):
        return value

    if isinstance(value, int):
        return bool(value)

    text = _normalize_lookup_text(value)

    if text in {"1", "true", "yes", "on", "enabled", "ready"}:
        return True

    if text in {"0", "false", "no", "off", "disabled", "not-ready"}:
        return False

    return bool(default)


def _safe_int(value: Any) -> Optional[int]:
    """Convert a value to int without raising."""
    if value is None or isinstance(value, bool):
        return None

    try:
        return int(value)
    except Exception:
        return None


def _safe_mapping(value: Any) -> dict[str, Any]:
    """Convert mapping/status-like objects into a plain dictionary."""
    if isinstance(value, Mapping):
        try:
            return dict(value)
        except Exception:
            return {}

    to_dict = getattr(value, "to_dict", None)

    if callable(to_dict):
        try:
            result = to_dict()
        except TypeError:
            try:
                result = to_dict(include_tracebacks=False)
            except Exception:
                return {}
        except Exception:
            return {}

        if isinstance(result, Mapping):
            return dict(result)

    return {}


def _safe_json(value: Any) -> Any:
    """Serialize diagnostics through the shared contract helper."""
    try:
        return make_json_safe(value)
    except Exception:
        if isinstance(value, Mapping):
            return {
                str(key): _safe_json(item)
                for key, item in value.items()
            }

        if isinstance(value, (list, tuple, set, frozenset)):
            return [_safe_json(item) for item in value]

        if value is None or isinstance(value, (str, int, float, bool)):
            return value

        try:
            return str(value)
        except Exception:
            return "<unserializable-value>"


def _safe_getattr(
    value: Any,
    name: str,
    default: Any = None,
) -> Any:
    """Read an attribute without allowing descriptor failures to escape."""
    try:
        return getattr(value, name, default)
    except Exception:
        return default


def _definition_system_id(definition: Any) -> Optional[str]:
    return _normalize_optional_text(
        _safe_getattr(definition, "system_block_id")
    )


def _definition_runtime_id(definition: Any) -> Optional[str]:
    return _normalize_optional_text(
        _safe_getattr(definition, "runtime_block_type_id")
    )


def _definition_aliases(definition: Any) -> tuple[str, ...]:
    return _normalize_string_tuple(
        _safe_getattr(definition, "aliases", ())
    )


def _definition_reserved_cell_value(definition: Any) -> Optional[int]:
    return _safe_int(
        _safe_getattr(definition, "reserved_cell_value")
    )


def _definition_is_persistent(definition: Any) -> bool:
    return _safe_bool(
        _safe_getattr(definition, "persist_as_block_type", False),
        False,
    )


def _definition_is_inventory_visible(definition: Any) -> bool:
    for attribute_name in (
        "can_appear_in_inventory",
        "inventory_visible",
        "show_in_inventory",
    ):
        value = _safe_getattr(definition, attribute_name, None)

        if value is not None:
            return _safe_bool(value, False)

    return False


def _definition_is_air(definition: Any) -> bool:
    explicit = _safe_getattr(definition, "is_air_state", None)

    if explicit is not None:
        return _safe_bool(explicit, False)

    return bool(
        _normalize_lookup_text(_definition_system_id(definition))
        == AIR_SYSTEM_BLOCK_ID
        and _definition_reserved_cell_value(definition) == AIR_CELL_VALUE
    )


def _definition_version(definition: Any) -> Optional[str]:
    return _normalize_optional_text(
        _safe_getattr(definition, "definition_version")
    )


def _definition_fingerprint(definition: Any) -> Optional[str]:
    return _normalize_optional_text(
        _safe_getattr(definition, "definition_fingerprint")
    )


def _serialize_definition_fallback(
    definition: Any,
    *,
    include_metadata: bool = True,
    include_internal: bool = False,
) -> dict[str, Any]:
    """Serialize a definition through its strongest available API."""
    for method_name in (
        "to_api_dict",
        "to_dict",
    ):
        method = _safe_getattr(definition, method_name)

        if not callable(method):
            continue

        attempts = (
            {
                "include_metadata": include_metadata,
                "include_internal": include_internal,
            },
            {"include_metadata": include_metadata},
            {},
        )

        for kwargs in attempts:
            try:
                value = method(**kwargs)
            except TypeError:
                continue
            except Exception:
                break

            if isinstance(value, Mapping):
                return dict(value)

    return {
        "systemBlockId": _definition_system_id(definition),
        "runtimeBlockTypeId": _definition_runtime_id(definition),
        "aliases": list(_definition_aliases(definition)),
        "definitionVersion": _definition_version(definition),
        "definitionFingerprint": _definition_fingerprint(definition),
        "reservedCellValue": _definition_reserved_cell_value(definition),
        "persistAsBlockType": _definition_is_persistent(definition),
        "inventoryVisible": _definition_is_inventory_visible(definition),
        "isAir": _definition_is_air(definition),
    }


def _extract_status_ready(value: Any) -> tuple[Optional[bool], dict[str, Any]]:
    """Extract readiness from a bool, mapping or status-like object."""
    if isinstance(value, bool):
        return value, {"ready": value}

    details = _safe_mapping(value)

    if details:
        raw_ready = details.get("ready")

        if raw_ready is not None:
            return _safe_bool(raw_ready, False), details

    raw_ready = _safe_getattr(value, "ready", None)

    if raw_ready is not None:
        return _safe_bool(raw_ready, False), details

    return None, details


# -----------------------------------------------------------------------------
# Exceptions
# -----------------------------------------------------------------------------

class SystemBlockCatalogError(RuntimeError):
    """Base error for the built-in system-block catalog."""


class SystemBlockProviderError(SystemBlockCatalogError):
    """Raised when one code-owned provider cannot be loaded or validated."""


class SystemBlockCatalogValidationError(SystemBlockCatalogError):
    """Raised when definitions or catalog indexes violate invariants."""

    def __init__(self, errors: Iterable[Any] | Any) -> None:
        self.errors = _normalize_error_messages(errors)
        details = "; ".join(self.errors) or "unknown catalog validation error"
        super().__init__(f"Invalid system-block catalog: {details}")


class SystemBlockLookupError(SystemBlockCatalogError, LookupError):
    """Raised when a required system-block identity is unknown."""

    def __init__(self, identifier: Any) -> None:
        self.identifier = _normalize_optional_text(identifier)
        super().__init__(
            f"Unknown built-in system-block identifier: "
            f"{self.identifier or '<empty>'}."
        )


class SystemBlockCatalogNotReadyError(SystemBlockCatalogError):
    """Raised when one or more required providers are not ready."""

    def __init__(self, status: Any) -> None:
        self.status = status
        details = _safe_mapping(status)
        errors = _normalize_error_messages(details.get("errors"))

        if not errors:
            errors = _normalize_error_messages(
                _safe_getattr(status, "errors", ())
            )

        message = "; ".join(errors) or "system-block catalog is not ready"
        super().__init__(message)


# -----------------------------------------------------------------------------
# Provider declarations and statuses
# -----------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class SystemBlockProviderSpec:
    """Immutable declaration for one built-in definition provider."""

    provider_key: str
    package_segment: str
    definition_factory_name: str

    expected_system_block_id: str
    expected_runtime_block_type_id: Optional[str]
    expected_reserved_cell_value: Optional[int]
    expected_persistent: bool

    readiness_factory_name: Optional[str] = None
    serializer_name: Optional[str] = None
    cache_clear_name: Optional[str] = None

    aliases: tuple[str, ...] = field(default_factory=tuple)

    required: bool = True
    order: int = 0
    description: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "provider_key",
            _normalize_required_text(
                self.provider_key,
                field_name="provider_key",
            ).casefold(),
        )
        object.__setattr__(
            self,
            "package_segment",
            _normalize_required_text(
                self.package_segment,
                field_name="package_segment",
            ),
        )
        object.__setattr__(
            self,
            "definition_factory_name",
            _normalize_required_text(
                self.definition_factory_name,
                field_name="definition_factory_name",
            ),
        )
        object.__setattr__(
            self,
            "expected_system_block_id",
            _normalize_required_text(
                self.expected_system_block_id,
                field_name="expected_system_block_id",
            ),
        )
        object.__setattr__(
            self,
            "expected_runtime_block_type_id",
            _normalize_optional_text(
                self.expected_runtime_block_type_id
            ),
        )
        object.__setattr__(
            self,
            "expected_reserved_cell_value",
            (
                _safe_int(self.expected_reserved_cell_value)
                if self.expected_reserved_cell_value is not None
                else None
            ),
        )
        object.__setattr__(
            self,
            "expected_persistent",
            bool(self.expected_persistent),
        )
        object.__setattr__(
            self,
            "readiness_factory_name",
            _normalize_optional_text(self.readiness_factory_name),
        )
        object.__setattr__(
            self,
            "serializer_name",
            _normalize_optional_text(self.serializer_name),
        )
        object.__setattr__(
            self,
            "cache_clear_name",
            _normalize_optional_text(self.cache_clear_name),
        )
        object.__setattr__(
            self,
            "aliases",
            _normalize_string_tuple(self.aliases),
        )
        object.__setattr__(self, "required", bool(self.required))
        object.__setattr__(self, "order", int(self.order))
        object.__setattr__(
            self,
            "description",
            _normalize_optional_text(self.description) or "",
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "providerKey": self.provider_key,
            "packageSegment": self.package_segment,
            "definitionFactoryName": self.definition_factory_name,
            "readinessFactoryName": self.readiness_factory_name,
            "serializerName": self.serializer_name,
            "cacheClearName": self.cache_clear_name,
            "expectedSystemBlockId": self.expected_system_block_id,
            "expectedRuntimeBlockTypeId": self.expected_runtime_block_type_id,
            "expectedReservedCellValue": self.expected_reserved_cell_value,
            "expectedPersistent": self.expected_persistent,
            "aliases": list(self.aliases),
            "required": self.required,
            "order": self.order,
            "description": self.description,
        }


@dataclass(frozen=True, slots=True)
class SystemBlockProviderImportAttempt:
    """One provider-module import attempt."""

    provider_key: str
    import_path: str
    imported: bool
    error_type: Optional[str] = None
    error: Optional[str] = None
    traceback_text: Optional[str] = field(default=None, repr=False)

    def to_dict(self, *, include_traceback: bool = False) -> dict[str, Any]:
        return {
            "providerKey": self.provider_key,
            "importPath": self.import_path,
            "imported": self.imported,
            "errorType": self.error_type,
            "error": self.error,
            "traceback": self.traceback_text if include_traceback else None,
        }


@dataclass(frozen=True, slots=True)
class SystemBlockProviderStatus:
    """Readiness status for one built-in definition provider."""

    provider_key: str
    required: bool
    ready: bool
    imported: bool

    module_name: Optional[str]
    module_path: Optional[str]

    system_block_id: Optional[str]
    runtime_block_type_id: Optional[str]
    definition_version: Optional[str]
    definition_fingerprint: Optional[str]

    readiness_checked: bool
    readiness_details: Mapping[str, Any]

    errors: tuple[str, ...]
    import_attempts: tuple[SystemBlockProviderImportAttempt, ...]

    def to_dict(self, *, include_tracebacks: bool = False) -> dict[str, Any]:
        return {
            "schemaVersion": SYSTEM_BLOCK_PROVIDER_STATUS_SCHEMA_VERSION,
            "providerKey": self.provider_key,
            "required": self.required,
            "ready": self.ready,
            "imported": self.imported,
            "moduleName": self.module_name,
            "modulePath": self.module_path,
            "systemBlockId": self.system_block_id,
            "runtimeBlockTypeId": self.runtime_block_type_id,
            "definitionVersion": self.definition_version,
            "definitionFingerprint": self.definition_fingerprint,
            "readinessChecked": self.readiness_checked,
            "readinessDetails": _safe_json(self.readiness_details),
            "errors": list(self.errors),
            "importAttempts": [
                attempt.to_dict(include_traceback=include_tracebacks)
                for attempt in self.import_attempts
            ],
        }


@dataclass(frozen=True, slots=True)
class SystemBlockCatalogStatus:
    """Complete non-raising catalog readiness status."""

    ready: bool
    catalog_id: str
    catalog_version: str

    provider_count: int
    ready_provider_count: int
    required_provider_count: int

    definition_count: int
    persistent_definition_count: int
    reserved_definition_count: int
    inventory_definition_count: int

    provider_statuses: tuple[SystemBlockProviderStatus, ...]
    errors: tuple[str, ...]

    def to_dict(self, *, include_tracebacks: bool = False) -> dict[str, Any]:
        return {
            "schemaVersion": SYSTEM_BLOCK_CATALOG_STATUS_SCHEMA_VERSION,
            "ready": self.ready,
            "catalogId": self.catalog_id,
            "catalogVersion": self.catalog_version,
            "moduleVersion": SYSTEM_BLOCK_CATALOG_MODULE_VERSION,
            "providerCount": self.provider_count,
            "readyProviderCount": self.ready_provider_count,
            "requiredProviderCount": self.required_provider_count,
            "definitionCount": self.definition_count,
            "persistentDefinitionCount": self.persistent_definition_count,
            "reservedDefinitionCount": self.reserved_definition_count,
            "inventoryDefinitionCount": self.inventory_definition_count,
            "providerStatuses": [
                status.to_dict(include_tracebacks=include_tracebacks)
                for status in self.provider_statuses
            ],
            "errors": list(self.errors),
        }


# -----------------------------------------------------------------------------
# Provider specs
#
# Helper functions above are already available when these dataclass instances
# execute __post_init__.
# -----------------------------------------------------------------------------

SYSTEM_BLOCK_PROVIDER_SPECS: Final[tuple[SystemBlockProviderSpec, ...]] = (
    SystemBlockProviderSpec(
        provider_key=AIR_PROVIDER_KEY,
        package_segment="air",
        definition_factory_name="get_air_definition",
        readiness_factory_name="get_air_definition_status",
        serializer_name="serialize_air_definition",
        cache_clear_name="clear_air_package_caches",
        expected_system_block_id=AIR_SYSTEM_BLOCK_ID,
        expected_runtime_block_type_id=None,
        expected_reserved_cell_value=AIR_CELL_VALUE,
        expected_persistent=False,
        aliases=("air", "empty", "empty_cell"),
        required=True,
        order=0,
        description="Reserved empty-cell definition.",
    ),
    SystemBlockProviderSpec(
        provider_key=RAILING_PROVIDER_KEY,
        package_segment="railing",
        definition_factory_name="get_railing_definition",
        readiness_factory_name="get_railing_definition_status",
        serializer_name="serialize_railing_for_system_catalog",
        cache_clear_name="clear_railing_package_caches",
        expected_system_block_id=RAILING_SYSTEM_BLOCK_ID,
        expected_runtime_block_type_id=RAILING_RUNTIME_BLOCK_TYPE_ID,
        expected_reserved_cell_value=None,
        expected_persistent=True,
        aliases=("railing",),
        required=True,
        order=100,
        description="Persistent built-in Railing block definition.",
    ),
)


# -----------------------------------------------------------------------------
# Import diagnostics state
# -----------------------------------------------------------------------------

_IMPORT_ATTEMPTS_LOCK = threading.RLock()
_PROVIDER_IMPORT_ATTEMPTS: dict[
    str,
    list[SystemBlockProviderImportAttempt],
] = {}


def _record_provider_import_attempt(
    attempt: SystemBlockProviderImportAttempt,
) -> None:
    try:
        with _IMPORT_ATTEMPTS_LOCK:
            _PROVIDER_IMPORT_ATTEMPTS.setdefault(
                attempt.provider_key,
                [],
            ).append(attempt)
    except Exception:
        pass


def _get_provider_import_attempts(
    provider_key: str,
) -> tuple[SystemBlockProviderImportAttempt, ...]:
    try:
        with _IMPORT_ATTEMPTS_LOCK:
            return tuple(
                _PROVIDER_IMPORT_ATTEMPTS.get(provider_key, ())
            )
    except Exception:
        return tuple()


# -----------------------------------------------------------------------------
# Provider specification indexes
# -----------------------------------------------------------------------------

@lru_cache(maxsize=1)
def get_system_block_provider_specs() -> tuple[SystemBlockProviderSpec, ...]:
    """Return provider specs in deterministic order."""
    return tuple(
        sorted(
            SYSTEM_BLOCK_PROVIDER_SPECS,
            key=lambda spec: (spec.order, spec.provider_key),
        )
    )


@lru_cache(maxsize=1)
def _get_provider_spec_map() -> Mapping[str, SystemBlockProviderSpec]:
    result: dict[str, SystemBlockProviderSpec] = {}
    errors: list[str] = []

    for spec in get_system_block_provider_specs():
        key = _normalize_lookup_text(spec.provider_key)

        if key is None:
            errors.append("Provider key resolved to empty text.")
            continue

        if key in result:
            errors.append(f"Duplicate provider key '{spec.provider_key}'.")
            continue

        result[key] = spec

    if errors:
        raise SystemBlockCatalogValidationError(errors)

    return MappingProxyType(result)


def _require_provider_spec(provider_key: Any) -> SystemBlockProviderSpec:
    key = _normalize_lookup_text(provider_key)

    if key is None:
        raise SystemBlockProviderError("Provider key is required.")

    spec = _get_provider_spec_map().get(key)

    if spec is None:
        raise SystemBlockProviderError(
            f"Unknown system-block provider '{provider_key}'."
        )

    return spec


# -----------------------------------------------------------------------------
# Provider module and definition loading
# -----------------------------------------------------------------------------

@lru_cache(maxsize=32)
def _get_provider_import_paths(provider_key: str) -> tuple[str, ...]:
    spec = _require_provider_spec(provider_key)
    candidates: list[str] = []

    current_package = _normalize_optional_text(__package__)

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


@lru_cache(maxsize=32)
def _load_provider_module(provider_key: str) -> ModuleType:
    spec = _require_provider_spec(provider_key)
    import_errors: list[str] = []

    for import_path in _get_provider_import_paths(spec.provider_key):
        try:
            module = importlib.import_module(import_path)
        except Exception as exc:
            error_text = _safe_exception_text(exc)
            import_errors.append(
                f"{import_path}: {type(exc).__name__}: {error_text}"
            )
            _record_provider_import_attempt(
                SystemBlockProviderImportAttempt(
                    provider_key=spec.provider_key,
                    import_path=import_path,
                    imported=False,
                    error_type=type(exc).__name__,
                    error=error_text,
                    traceback_text=traceback.format_exc(),
                )
            )
            continue

        if not isinstance(module, ModuleType):
            error_text = "importlib returned a non-module object"
            import_errors.append(f"{import_path}: TypeError: {error_text}")
            _record_provider_import_attempt(
                SystemBlockProviderImportAttempt(
                    provider_key=spec.provider_key,
                    import_path=import_path,
                    imported=False,
                    error_type="TypeError",
                    error=error_text,
                )
            )
            continue

        _record_provider_import_attempt(
            SystemBlockProviderImportAttempt(
                provider_key=spec.provider_key,
                import_path=import_path,
                imported=True,
            )
        )
        return module

    raise SystemBlockProviderError(
        f"Could not import provider '{spec.provider_key}'. "
        + " | ".join(import_errors)
    )


def _collect_provider_definition_errors(
    spec: SystemBlockProviderSpec,
    definition: Any,
) -> tuple[str, ...]:
    errors: list[str] = []

    try:
        require_system_block_definition(definition)
    except Exception as exc:
        errors.append(
            f"Provider '{spec.provider_key}' returned an invalid definition: "
            f"{type(exc).__name__}: {_safe_exception_text(exc)}"
        )

    actual_system_id = _definition_system_id(definition)
    actual_runtime_id = _definition_runtime_id(definition)
    actual_reserved = _definition_reserved_cell_value(definition)
    actual_persistent = _definition_is_persistent(definition)

    if _normalize_lookup_text(actual_system_id) != _normalize_lookup_text(
        spec.expected_system_block_id
    ):
        errors.append(
            f"Provider '{spec.provider_key}' returned system id "
            f"'{actual_system_id}', expected '{spec.expected_system_block_id}'."
        )

    if _normalize_lookup_text(actual_runtime_id) != _normalize_lookup_text(
        spec.expected_runtime_block_type_id
    ):
        errors.append(
            f"Provider '{spec.provider_key}' returned runtime id "
            f"'{actual_runtime_id}', expected "
            f"'{spec.expected_runtime_block_type_id}'."
        )

    if actual_reserved != spec.expected_reserved_cell_value:
        errors.append(
            f"Provider '{spec.provider_key}' returned reserved cell value "
            f"'{actual_reserved}', expected "
            f"'{spec.expected_reserved_cell_value}'."
        )

    if actual_persistent != spec.expected_persistent:
        errors.append(
            f"Provider '{spec.provider_key}' persistence flag is "
            f"'{actual_persistent}', expected '{spec.expected_persistent}'."
        )

    return _normalize_error_messages(errors)


@lru_cache(maxsize=32)
def _load_provider_definition(provider_key: str) -> SystemBlockDefinition:
    spec = _require_provider_spec(provider_key)
    module = _load_provider_module(spec.provider_key)

    factory = _safe_getattr(module, spec.definition_factory_name)

    if not callable(factory):
        raise SystemBlockProviderError(
            f"Provider '{spec.provider_key}' does not expose callable "
            f"'{spec.definition_factory_name}'."
        )

    try:
        definition = factory()
    except Exception as exc:
        raise SystemBlockProviderError(
            f"Provider '{spec.provider_key}' definition factory failed: "
            f"{type(exc).__name__}: {_safe_exception_text(exc)}"
        ) from exc

    errors = _collect_provider_definition_errors(spec, definition)

    if errors:
        raise SystemBlockProviderError("; ".join(errors))

    return definition


# -----------------------------------------------------------------------------
# Immutable catalog object
# -----------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class SystemBlockCatalog:
    """Validated immutable index of all built-in system definitions."""

    catalog_id: str
    catalog_version: str
    definitions: tuple[SystemBlockDefinition, ...]

    _by_system_id: Mapping[str, SystemBlockDefinition] = field(
        init=False,
        repr=False,
        compare=False,
    )
    _by_runtime_id: Mapping[str, SystemBlockDefinition] = field(
        init=False,
        repr=False,
        compare=False,
    )
    _by_identifier: Mapping[str, SystemBlockDefinition] = field(
        init=False,
        repr=False,
        compare=False,
    )
    _by_reserved_cell_value: Mapping[int, SystemBlockDefinition] = field(
        init=False,
        repr=False,
        compare=False,
    )

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "catalog_id",
            _normalize_required_text(self.catalog_id, field_name="catalog_id"),
        )
        object.__setattr__(
            self,
            "catalog_version",
            _normalize_required_text(
                self.catalog_version,
                field_name="catalog_version",
            ),
        )
        object.__setattr__(self, "definitions", tuple(self.definitions))

        errors: list[str] = []
        by_system_id: dict[str, SystemBlockDefinition] = {}
        by_runtime_id: dict[str, SystemBlockDefinition] = {}
        by_identifier: dict[str, SystemBlockDefinition] = {}
        by_reserved: dict[int, SystemBlockDefinition] = {}

        if not self.definitions:
            errors.append("Catalog must contain at least one definition.")

        for definition in self.definitions:
            try:
                require_system_block_definition(definition)
            except Exception as exc:
                errors.append(
                    f"Invalid catalog definition: {type(exc).__name__}: "
                    f"{_safe_exception_text(exc)}"
                )
                continue

            system_id = _definition_system_id(definition)
            runtime_id = _definition_runtime_id(definition)
            reserved_value = _definition_reserved_cell_value(definition)
            persistent = _definition_is_persistent(definition)

            system_key = _normalize_lookup_text(system_id)

            if system_key is None:
                errors.append("Definition has no system_block_id.")
                continue

            if not system_key.startswith(SYSTEM_BLOCK_ID_PREFIX):
                errors.append(
                    f"System block id '{system_id}' must start with "
                    f"'{SYSTEM_BLOCK_ID_PREFIX}'."
                )

            if system_key in by_system_id:
                errors.append(f"Duplicate system block id '{system_id}'.")
            else:
                by_system_id[system_key] = definition

            if persistent and runtime_id is None:
                errors.append(
                    f"Persistent system block '{system_id}' has no runtime id."
                )

            if persistent and reserved_value is not None:
                errors.append(
                    f"Persistent system block '{system_id}' must not own a "
                    "globally reserved cell value."
                )

            if not persistent and runtime_id is not None:
                errors.append(
                    f"Non-persistent system block '{system_id}' unexpectedly "
                    f"has runtime id '{runtime_id}'."
                )

            runtime_key = _normalize_lookup_text(runtime_id)

            if runtime_key is not None:
                previous = by_runtime_id.get(runtime_key)

                if previous is not None and previous is not definition:
                    errors.append(f"Duplicate runtime block id '{runtime_id}'.")
                else:
                    by_runtime_id[runtime_key] = definition

            if reserved_value is not None:
                previous_reserved = by_reserved.get(reserved_value)

                if previous_reserved is not None and previous_reserved is not definition:
                    errors.append(
                        f"Duplicate reserved cell value '{reserved_value}'."
                    )
                else:
                    by_reserved[reserved_value] = definition

            identifiers = (
                system_id,
                runtime_id,
                *_definition_aliases(definition),
            )

            for identifier in identifiers:
                identifier_key = _normalize_lookup_text(identifier)

                if identifier_key is None:
                    continue

                previous = by_identifier.get(identifier_key)

                if previous is not None and previous is not definition:
                    errors.append(
                        f"Identifier collision '{identifier}' between "
                        f"'{_definition_system_id(previous)}' and '{system_id}'."
                    )
                    continue

                by_identifier[identifier_key] = definition

        air_definition = by_reserved.get(AIR_CELL_VALUE)

        if air_definition is None:
            errors.append(
                f"Catalog must contain Air at reserved cell value "
                f"{AIR_CELL_VALUE}."
            )
        elif not _definition_is_air(air_definition):
            errors.append(
                f"Reserved cell value {AIR_CELL_VALUE} is not owned by Air."
            )

        if len(by_reserved) != 1:
            errors.append(
                "The initial catalog must contain exactly one reserved cell "
                "state: Air at cellValue 0."
            )

        if errors:
            raise SystemBlockCatalogValidationError(errors)

        object.__setattr__(
            self,
            "_by_system_id",
            MappingProxyType(by_system_id),
        )
        object.__setattr__(
            self,
            "_by_runtime_id",
            MappingProxyType(by_runtime_id),
        )
        object.__setattr__(
            self,
            "_by_identifier",
            MappingProxyType(by_identifier),
        )
        object.__setattr__(
            self,
            "_by_reserved_cell_value",
            MappingProxyType(by_reserved),
        )

    @property
    def definition_count(self) -> int:
        return len(self.definitions)

    @property
    def persistent_definitions(self) -> tuple[SystemBlockDefinition, ...]:
        return tuple(
            definition
            for definition in self.definitions
            if _definition_is_persistent(definition)
        )

    @property
    def reserved_definitions(self) -> tuple[SystemBlockDefinition, ...]:
        return tuple(
            definition
            for definition in self.definitions
            if _definition_reserved_cell_value(definition) is not None
        )

    @property
    def inventory_definitions(self) -> tuple[SystemBlockDefinition, ...]:
        return tuple(
            definition
            for definition in self.definitions
            if _definition_is_inventory_visible(definition)
        )

    def get(self, identifier: Any) -> Optional[SystemBlockDefinition]:
        key = _normalize_lookup_text(identifier)
        return self._by_identifier.get(key) if key is not None else None

    def require(self, identifier: Any) -> SystemBlockDefinition:
        definition = self.get(identifier)

        if definition is None:
            raise SystemBlockLookupError(identifier)

        return definition

    def get_by_system_id(self, identifier: Any) -> Optional[SystemBlockDefinition]:
        key = _normalize_lookup_text(identifier)
        return self._by_system_id.get(key) if key is not None else None

    def get_by_runtime_id(self, identifier: Any) -> Optional[SystemBlockDefinition]:
        key = _normalize_lookup_text(identifier)
        return self._by_runtime_id.get(key) if key is not None else None

    def get_by_reserved_cell_value(
        self,
        cell_value: Any,
    ) -> Optional[SystemBlockDefinition]:
        normalized = _safe_int(cell_value)
        return (
            self._by_reserved_cell_value.get(normalized)
            if normalized is not None
            else None
        )

    def to_dict(
        self,
        *,
        include_definitions: bool = True,
        include_metadata: bool = True,
        include_internal: bool = False,
    ) -> dict[str, Any]:
        result = {
            "schemaVersion": SYSTEM_BLOCK_CATALOG_SCHEMA_VERSION,
            "catalogId": self.catalog_id,
            "catalogVersion": self.catalog_version,
            "moduleVersion": SYSTEM_BLOCK_CATALOG_MODULE_VERSION,
            "definitionCount": self.definition_count,
            "persistentDefinitionCount": len(self.persistent_definitions),
            "reservedDefinitionCount": len(self.reserved_definitions),
            "inventoryDefinitionCount": len(self.inventory_definitions),
            "airCellValue": AIR_CELL_VALUE,
        }

        if include_definitions:
            result["definitions"] = [
                _serialize_definition_from_provider(
                    definition,
                    include_metadata=include_metadata,
                    include_internal=include_internal,
                )
                for definition in self.definitions
            ]

        return result


# -----------------------------------------------------------------------------
# Catalog construction and lookup
# -----------------------------------------------------------------------------

@lru_cache(maxsize=1)
def get_system_block_catalog() -> SystemBlockCatalog:
    """Load all providers and return the validated immutable catalog."""
    definitions: list[SystemBlockDefinition] = []
    errors: list[str] = []

    for spec in get_system_block_provider_specs():
        try:
            definitions.append(_load_provider_definition(spec.provider_key))
        except Exception as exc:
            if spec.required:
                errors.append(
                    f"Provider '{spec.provider_key}' failed: "
                    f"{type(exc).__name__}: {_safe_exception_text(exc)}"
                )

    if errors:
        raise SystemBlockCatalogValidationError(errors)

    return SystemBlockCatalog(
        catalog_id=SYSTEM_BLOCK_CATALOG_ID,
        catalog_version=SYSTEM_BLOCK_CATALOG_VERSION,
        definitions=tuple(definitions),
    )


def get_system_block_definitions() -> tuple[SystemBlockDefinition, ...]:
    return get_system_block_catalog().definitions


def get_persistent_system_block_definitions() -> tuple[SystemBlockDefinition, ...]:
    return get_system_block_catalog().persistent_definitions


def get_reserved_system_block_definitions() -> tuple[SystemBlockDefinition, ...]:
    return get_system_block_catalog().reserved_definitions


def get_inventory_system_block_definitions() -> tuple[SystemBlockDefinition, ...]:
    return get_system_block_catalog().inventory_definitions


def get_system_block_definition(identifier: Any) -> Optional[SystemBlockDefinition]:
    return get_system_block_catalog().get(identifier)


def require_system_block_definition_from_catalog(
    identifier: Any,
) -> SystemBlockDefinition:
    return get_system_block_catalog().require(identifier)


def get_air_system_block_definition() -> SystemBlockDefinition:
    definition = get_system_block_catalog().get_by_reserved_cell_value(
        AIR_CELL_VALUE
    )

    if definition is None or not _definition_is_air(definition):
        raise SystemBlockLookupError(AIR_SYSTEM_BLOCK_ID)

    return definition


def get_railing_system_block_definition() -> SystemBlockDefinition:
    return get_system_block_catalog().require(RAILING_SYSTEM_BLOCK_ID)


def get_reserved_system_block_definition_for_cell_value(
    cell_value: Any,
) -> Optional[SystemBlockDefinition]:
    return get_system_block_catalog().get_by_reserved_cell_value(cell_value)


# -----------------------------------------------------------------------------
# Identity helpers
# -----------------------------------------------------------------------------

@lru_cache(maxsize=1)
def get_reserved_system_identifiers() -> frozenset[str]:
    identifiers: set[str] = set()

    for definition in get_reserved_system_block_definitions():
        for value in (
            _definition_system_id(definition),
            _definition_runtime_id(definition),
            *_definition_aliases(definition),
        ):
            normalized = _normalize_lookup_text(value)

            if normalized:
                identifiers.add(normalized)

    for spec in get_system_block_provider_specs():
        if spec.expected_reserved_cell_value is None:
            continue

        for value in (spec.expected_system_block_id, *spec.aliases):
            normalized = _normalize_lookup_text(value)

            if normalized:
                identifiers.add(normalized)

    return frozenset(identifiers)


def is_system_block_identifier(identifier: Any) -> bool:
    try:
        return get_system_block_catalog().get(identifier) is not None
    except Exception:
        return False


def is_system_runtime_block_type_id(identifier: Any) -> bool:
    try:
        return get_system_block_catalog().get_by_runtime_id(identifier) is not None
    except Exception:
        return False


def is_reserved_system_identifier(identifier: Any) -> bool:
    normalized = _normalize_lookup_text(identifier)
    return bool(normalized and normalized in get_reserved_system_identifiers())


def is_reserved_cell_state_identifier(identifier: Any) -> bool:
    return is_reserved_system_identifier(identifier)


def requires_remove_block_for_identifier(identifier: Any) -> bool:
    """Return whether the identity must be produced through RemoveBlock."""
    return is_reserved_cell_state_identifier(identifier)


def canonical_runtime_block_type_id(
    identifier: Any,
    *,
    strict: bool = False,
) -> Optional[str]:
    """
    Resolve a system identity to its canonical runtime BlockType id.

    Air resolves to ``None``. Unknown non-system ids are preserved when
    ``strict`` is false so this helper can safely participate in mixed normal
    and system block paths.
    """
    normalized_text = _normalize_optional_text(identifier)

    if normalized_text is None:
        if strict:
            raise SystemBlockLookupError(identifier)
        return None

    try:
        definition = get_system_block_catalog().get(normalized_text)
    except Exception:
        definition = None

    if definition is not None:
        return _definition_runtime_id(definition)

    if strict:
        raise SystemBlockLookupError(identifier)

    return normalized_text


# -----------------------------------------------------------------------------
# Persistence projection
# -----------------------------------------------------------------------------

@lru_cache(maxsize=1)
def get_persistent_system_block_values_by_runtime_id(
) -> Mapping[str, Mapping[str, Any]]:
    """Return immutable canonical BlockType values keyed by runtime id."""
    result: dict[str, Mapping[str, Any]] = {}

    for definition in get_persistent_system_block_definitions():
        runtime_id = _definition_runtime_id(definition)

        if runtime_id is None:
            raise SystemBlockCatalogValidationError(
                f"Persistent definition '{_definition_system_id(definition)}' "
                "has no runtime id."
            )

        factory = _safe_getattr(definition, "to_persistent_block_values")

        if not callable(factory):
            raise SystemBlockCatalogValidationError(
                f"Definition '{_definition_system_id(definition)}' has no "
                "to_persistent_block_values() method."
            )

        try:
            values = factory(include_metadata=True)
        except TypeError:
            values = factory()

        if not isinstance(values, Mapping):
            raise SystemBlockCatalogValidationError(
                f"Persistent values for '{runtime_id}' are not a mapping."
            )

        result[runtime_id] = MappingProxyType(dict(values))

    return MappingProxyType(result)


# -----------------------------------------------------------------------------
# Provider-aware serialization
# -----------------------------------------------------------------------------

@lru_cache(maxsize=256)
def _provider_key_for_system_id(system_block_id: str) -> Optional[str]:
    lookup = _normalize_lookup_text(system_block_id)

    for spec in get_system_block_provider_specs():
        if _normalize_lookup_text(spec.expected_system_block_id) == lookup:
            return spec.provider_key

    return None


def _serialize_definition_from_provider(
    definition: SystemBlockDefinition,
    *,
    include_metadata: bool,
    include_internal: bool,
) -> dict[str, Any]:
    system_id = _definition_system_id(definition)
    provider_key = (
        _provider_key_for_system_id(system_id)
        if system_id is not None
        else None
    )

    if provider_key is not None:
        spec = _require_provider_spec(provider_key)

        try:
            module = _load_provider_module(provider_key)
            serializer = (
                _safe_getattr(module, spec.serializer_name)
                if spec.serializer_name
                else None
            )

            if callable(serializer):
                attempts = (
                    {
                        "include_metadata": include_metadata,
                        "include_internal": include_internal,
                    },
                    {"include_metadata": include_metadata},
                    {},
                )

                for kwargs in attempts:
                    try:
                        value = serializer(**kwargs)
                    except TypeError:
                        try:
                            value = serializer(definition, **kwargs)
                        except TypeError:
                            continue
                        except Exception:
                            break
                    except Exception:
                        break

                    if isinstance(value, Mapping):
                        result = dict(value)
                        result.setdefault("providerKey", provider_key)
                        return result
        except Exception:
            pass

    result = _serialize_definition_fallback(
        definition,
        include_metadata=include_metadata,
        include_internal=include_internal,
    )

    if provider_key is not None:
        result.setdefault("providerKey", provider_key)

    return result


@lru_cache(maxsize=256)
def _serialize_system_block_definition_cached(
    system_block_id: str,
    definition_fingerprint: str,
    include_metadata: bool,
    include_internal: bool,
) -> Mapping[str, Any]:
    definition = require_system_block_definition_from_catalog(system_block_id)

    current_fingerprint = _definition_fingerprint(definition) or ""

    if current_fingerprint != definition_fingerprint:
        raise SystemBlockCatalogError(
            f"Definition fingerprint changed while serializing "
            f"'{system_block_id}'."
        )

    return MappingProxyType(
        _serialize_definition_from_provider(
            definition,
            include_metadata=include_metadata,
            include_internal=include_internal,
        )
    )


def serialize_system_block_definition_from_catalog(
    identifier: Any,
    *,
    include_metadata: bool = True,
    include_internal: bool = False,
) -> dict[str, Any]:
    definition = require_system_block_definition_from_catalog(identifier)
    system_id = _definition_system_id(definition)

    if system_id is None:
        raise SystemBlockCatalogValidationError(
            "Resolved definition has no system block id."
        )

    fingerprint = _definition_fingerprint(definition) or ""

    return dict(
        _serialize_system_block_definition_cached(
            system_id,
            fingerprint,
            bool(include_metadata),
            bool(include_internal),
        )
    )


def serialize_system_block_catalog(
    *,
    include_definitions: bool = True,
    include_metadata: bool = True,
    include_internal: bool = False,
    include_status: bool = True,
) -> dict[str, Any]:
    catalog = get_system_block_catalog()
    result = catalog.to_dict(
        include_definitions=include_definitions,
        include_metadata=include_metadata,
        include_internal=include_internal,
    )

    if include_status:
        result["status"] = get_system_block_catalog_status().to_dict()

    return result


# -----------------------------------------------------------------------------
# Readiness diagnostics
# -----------------------------------------------------------------------------

@lru_cache(maxsize=32)
def _get_provider_status(provider_key: str) -> SystemBlockProviderStatus:
    spec = _require_provider_spec(provider_key)
    errors: list[str] = []

    try:
        module = _load_provider_module(spec.provider_key)
    except Exception as exc:
        errors.append(
            f"Provider import failed: {type(exc).__name__}: "
            f"{_safe_exception_text(exc)}"
        )
        return SystemBlockProviderStatus(
            provider_key=spec.provider_key,
            required=spec.required,
            ready=False,
            imported=False,
            module_name=None,
            module_path=None,
            system_block_id=None,
            runtime_block_type_id=None,
            definition_version=None,
            definition_fingerprint=None,
            readiness_checked=False,
            readiness_details=MappingProxyType({}),
            errors=_normalize_error_messages(errors),
            import_attempts=_get_provider_import_attempts(spec.provider_key),
        )

    definition: Optional[SystemBlockDefinition] = None

    try:
        definition = _load_provider_definition(spec.provider_key)
    except Exception as exc:
        errors.append(
            f"Provider definition failed: {type(exc).__name__}: "
            f"{_safe_exception_text(exc)}"
        )

    readiness_checked = False
    readiness_ready: Optional[bool] = None
    readiness_details: dict[str, Any] = {}

    if spec.readiness_factory_name:
        readiness_checked = True
        readiness_factory = _safe_getattr(
            module,
            spec.readiness_factory_name,
        )

        if not callable(readiness_factory):
            errors.append(
                f"Readiness factory '{spec.readiness_factory_name}' is missing."
            )
            readiness_ready = False
        else:
            try:
                raw_status = readiness_factory()
                readiness_ready, readiness_details = _extract_status_ready(
                    raw_status
                )
            except Exception as exc:
                readiness_ready = False
                errors.append(
                    f"Readiness factory '{spec.readiness_factory_name}' "
                    f"failed: {type(exc).__name__}: "
                    f"{_safe_exception_text(exc)}"
                )

            if readiness_ready is None:
                readiness_ready = False
                errors.append(
                    f"Readiness factory '{spec.readiness_factory_name}' did "
                    "not provide a boolean ready value."
                )
            elif readiness_ready is False:
                errors.extend(
                    _normalize_error_messages(readiness_details.get("errors"))
                )
                if readiness_details.get("error"):
                    errors.append(str(readiness_details["error"]))

    normalized_errors = _normalize_error_messages(errors)
    ready = bool(
        definition is not None
        and (not readiness_checked or readiness_ready is True)
        and not normalized_errors
    )

    return SystemBlockProviderStatus(
        provider_key=spec.provider_key,
        required=spec.required,
        ready=ready,
        imported=True,
        module_name=_normalize_optional_text(
            _safe_getattr(module, "__name__")
        ),
        module_path=_normalize_optional_text(
            _safe_getattr(module, "__file__")
        ),
        system_block_id=(
            _definition_system_id(definition)
            if definition is not None
            else None
        ),
        runtime_block_type_id=(
            _definition_runtime_id(definition)
            if definition is not None
            else None
        ),
        definition_version=(
            _definition_version(definition)
            if definition is not None
            else None
        ),
        definition_fingerprint=(
            _definition_fingerprint(definition)
            if definition is not None
            else None
        ),
        readiness_checked=readiness_checked,
        readiness_details=MappingProxyType(readiness_details),
        errors=normalized_errors,
        import_attempts=_get_provider_import_attempts(spec.provider_key),
    )


@lru_cache(maxsize=1)
def get_system_block_catalog_status() -> SystemBlockCatalogStatus:
    """Return complete non-raising catalog readiness diagnostics."""
    statuses: list[SystemBlockProviderStatus] = []
    errors: list[str] = []

    for spec in get_system_block_provider_specs():
        try:
            status = _get_provider_status(spec.provider_key)
        except Exception as exc:
            status = SystemBlockProviderStatus(
                provider_key=spec.provider_key,
                required=spec.required,
                ready=False,
                imported=False,
                module_name=None,
                module_path=None,
                system_block_id=None,
                runtime_block_type_id=None,
                definition_version=None,
                definition_fingerprint=None,
                readiness_checked=False,
                readiness_details=MappingProxyType({}),
                errors=(
                    f"Could not build provider status: "
                    f"{type(exc).__name__}: {_safe_exception_text(exc)}",
                ),
                import_attempts=_get_provider_import_attempts(spec.provider_key),
            )

        statuses.append(status)

        if spec.required and not status.ready:
            errors.append(f"Required provider '{spec.provider_key}' is not ready.")
            errors.extend(status.errors)

    definition_count = 0
    persistent_count = 0
    reserved_count = 0
    inventory_count = 0

    try:
        catalog = get_system_block_catalog()
        definition_count = catalog.definition_count
        persistent_count = len(catalog.persistent_definitions)
        reserved_count = len(catalog.reserved_definitions)
        inventory_count = len(catalog.inventory_definitions)
    except Exception as exc:
        errors.append(
            f"Catalog construction failed: {type(exc).__name__}: "
            f"{_safe_exception_text(exc)}"
        )

    required_statuses = [status for status in statuses if status.required]
    normalized_errors = _normalize_error_messages(errors)
    ready = bool(
        required_statuses
        and all(status.ready for status in required_statuses)
        and definition_count == len(get_system_block_provider_specs())
        and persistent_count >= 1
        and reserved_count == 1
        and not normalized_errors
    )

    return SystemBlockCatalogStatus(
        ready=ready,
        catalog_id=SYSTEM_BLOCK_CATALOG_ID,
        catalog_version=SYSTEM_BLOCK_CATALOG_VERSION,
        provider_count=len(statuses),
        ready_provider_count=sum(1 for status in statuses if status.ready),
        required_provider_count=len(required_statuses),
        definition_count=definition_count,
        persistent_definition_count=persistent_count,
        reserved_definition_count=reserved_count,
        inventory_definition_count=inventory_count,
        provider_statuses=tuple(statuses),
        errors=normalized_errors,
    )


def collect_system_block_catalog_errors() -> tuple[str, ...]:
    return get_system_block_catalog_status().errors


def is_system_block_catalog_ready() -> bool:
    try:
        return bool(get_system_block_catalog_status().ready)
    except Exception:
        return False


def require_system_block_catalog_ready() -> SystemBlockCatalog:
    status = get_system_block_catalog_status()

    if not status.ready:
        raise SystemBlockCatalogNotReadyError(status)

    return get_system_block_catalog()


def get_system_block_catalog_debug_summary(
    *,
    include_tracebacks: bool = False,
) -> dict[str, Any]:
    try:
        return get_system_block_catalog_status().to_dict(
            include_tracebacks=include_tracebacks
        )
    except Exception as exc:
        return {
            "schemaVersion": SYSTEM_BLOCK_CATALOG_STATUS_SCHEMA_VERSION,
            "ready": False,
            "catalogId": SYSTEM_BLOCK_CATALOG_ID,
            "catalogVersion": SYSTEM_BLOCK_CATALOG_VERSION,
            "moduleVersion": SYSTEM_BLOCK_CATALOG_MODULE_VERSION,
            "errors": [
                f"Could not build catalog diagnostics: "
                f"{type(exc).__name__}: {_safe_exception_text(exc)}"
            ],
        }


@lru_cache(maxsize=1)
def get_system_block_catalog_descriptor() -> Mapping[str, Any]:
    return MappingProxyType(
        {
            "schemaVersion": SYSTEM_BLOCK_CATALOG_SCHEMA_VERSION,
            "statusSchemaVersion": SYSTEM_BLOCK_CATALOG_STATUS_SCHEMA_VERSION,
            "catalogId": SYSTEM_BLOCK_CATALOG_ID,
            "catalogVersion": SYSTEM_BLOCK_CATALOG_VERSION,
            "moduleVersion": SYSTEM_BLOCK_CATALOG_MODULE_VERSION,
            "systemBlockIdPrefix": SYSTEM_BLOCK_ID_PREFIX,
            "airSystemBlockId": AIR_SYSTEM_BLOCK_ID,
            "airCellValue": AIR_CELL_VALUE,
            "railingSystemBlockId": RAILING_SYSTEM_BLOCK_ID,
            "railingRuntimeBlockTypeId": RAILING_RUNTIME_BLOCK_TYPE_ID,
            "providerKeys": tuple(
                spec.provider_key for spec in get_system_block_provider_specs()
            ),
            "lazyProviderImports": True,
            "cachesDatabaseRows": False,
            "importsDatabaseModels": False,
        }
    )


# -----------------------------------------------------------------------------
# Cache management
# -----------------------------------------------------------------------------

def clear_system_block_catalog_caches(
    *,
    clear_provider_caches: bool = True,
    clear_import_attempts: bool = True,
) -> None:
    """Clear immutable catalog/provider caches for tests or development reloads."""
    loaded_modules: list[tuple[SystemBlockProviderSpec, ModuleType]] = []

    if clear_provider_caches:
        for spec in get_system_block_provider_specs():
            try:
                loaded_modules.append(
                    (spec, _load_provider_module(spec.provider_key))
                )
            except Exception:
                continue

        for spec, module in reversed(loaded_modules):
            clear_name = spec.cache_clear_name

            if not clear_name:
                continue

            clear_function = _safe_getattr(module, clear_name)

            if callable(clear_function):
                try:
                    clear_function()
                except Exception:
                    pass

    get_system_block_catalog_status.cache_clear()
    get_system_block_catalog.cache_clear()
    get_persistent_system_block_values_by_runtime_id.cache_clear()
    get_reserved_system_identifiers.cache_clear()
    get_system_block_catalog_descriptor.cache_clear()

    _get_provider_status.cache_clear()
    _serialize_system_block_definition_cached.cache_clear()
    _provider_key_for_system_id.cache_clear()
    _load_provider_definition.cache_clear()
    _load_provider_module.cache_clear()
    _get_provider_import_paths.cache_clear()
    _get_provider_spec_map.cache_clear()
    get_system_block_provider_specs.cache_clear()

    if clear_import_attempts:
        try:
            with _IMPORT_ATTEMPTS_LOCK:
                _PROVIDER_IMPORT_ATTEMPTS.clear()
        except Exception:
            pass


# -----------------------------------------------------------------------------
# Public exports
# -----------------------------------------------------------------------------

__all__ = [
    "AIR_PROVIDER_KEY",
    "AIR_SYSTEM_BLOCK_ID",
    "RAILING_PROVIDER_KEY",
    "RAILING_RUNTIME_BLOCK_TYPE_ID",
    "RAILING_SYSTEM_BLOCK_ID",
    "SYSTEM_BLOCK_CATALOG_ID",
    "SYSTEM_BLOCK_CATALOG_MODULE_VERSION",
    "SYSTEM_BLOCK_CATALOG_SCHEMA_VERSION",
    "SYSTEM_BLOCK_CATALOG_STATUS_SCHEMA_VERSION",
    "SYSTEM_BLOCK_CATALOG_VERSION",
    "SYSTEM_BLOCK_ID_PREFIX",
    "SYSTEM_BLOCK_PROVIDER_SPECS",
    "SystemBlockCatalog",
    "SystemBlockCatalogError",
    "SystemBlockCatalogNotReadyError",
    "SystemBlockCatalogStatus",
    "SystemBlockCatalogValidationError",
    "SystemBlockLookupError",
    "SystemBlockProviderError",
    "SystemBlockProviderImportAttempt",
    "SystemBlockProviderSpec",
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
    "get_system_block_provider_specs",
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
]
