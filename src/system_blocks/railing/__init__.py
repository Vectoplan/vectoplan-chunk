# services/vectoplan-chunk/src/system_blocks/railing/__init__.py
"""
Public package facade for the built-in VECTOPLAN Railing definition.

The canonical Railing definition is implemented in:

    src/system_blocks/railing/definition.py

Railing is a persistent built-in system block. Its semantic definition is owned
by ``vectoplan-chunk`` and is mirrored into the ``BlockRegistry`` assigned to a
concrete ``WorldInstance``.

Version 1 uses the existing generic block pipeline:

    SetBlock(system_railing)
    -> resolve persistent BlockType
    -> add or reuse chunk-local palette entry
    -> write cellValue = paletteIndex + 1
    -> persist ChunkSnapshot
    -> append ChunkEvent
    -> update WorldCommandLog

The package facade is deliberately lazy:

- importing ``src.system_blocks.railing`` does not immediately construct the
  Railing definition,
- the definition module is imported only when an exported symbol is accessed,
- resolved modules and exports are cached,
- import failures remain available through diagnostics,
- package readiness can be checked without database or Flask dependencies,
- tests and development tooling can explicitly clear all local caches.

Important boundaries:

- no Flask imports
- no SQLAlchemy imports
- no database access
- no database commits
- no route registration
- no bootstrap mutation
- no fixed global Railing cell value
- no import-time BlockType creation

The actual runtime cell value of Railing is always determined by the concrete
chunk-local palette:

    cellValue = paletteIndex + 1
"""

from __future__ import annotations

import importlib
import traceback
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from functools import lru_cache
from types import MappingProxyType, ModuleType
from typing import Any, Final, Optional, TYPE_CHECKING


# -----------------------------------------------------------------------------
# Static package metadata
# -----------------------------------------------------------------------------

RAILING_PACKAGE_NAME: Final[str] = "src.system_blocks.railing"
RAILING_PACKAGE_VERSION: Final[str] = "1.0.0"

RAILING_DEFINITION_MODULE_NAME: Final[str] = "definition"

RAILING_PACKAGE_STATUS_SCHEMA_VERSION: Final[str] = (
    "system-block-railing-package-status.schema.v1"
)

RAILING_PACKAGE_SOURCE_PATH: Final[str] = (
    "services/vectoplan-chunk/src/system_blocks/railing"
)

RAILING_DEFINITION_SOURCE_PATH: Final[str] = (
    "services/vectoplan-chunk/src/system_blocks/railing/definition.py"
)


# -----------------------------------------------------------------------------
# Public exports delegated to definition.py
#
# Keep this tuple synchronized with definition.py.__all__. Package readiness
# reports missing symbols instead of silently hiding an incomplete definition.
# -----------------------------------------------------------------------------

RAILING_DEFINITION_EXPORTS: Final[tuple[str, ...]] = (
    "RAILING_BREAKABLE",
    "RAILING_COLLIDABLE",
    "RAILING_CURRENT_COLLISION",
    "RAILING_CURRENT_GEOMETRY",
    "RAILING_DEFAULT_PALETTE_INDEX",
    "RAILING_DEFINITION_MODULE_VERSION",
    "RAILING_DEFINITION_VERSION",
    "RAILING_DESCRIPTION",
    "RAILING_EMITS_LIGHT",
    "RAILING_FUTURE_GEOMETRY",
    "RAILING_HARDNESS",
    "RAILING_ICON_ID",
    "RAILING_IMMUTABLE_DEFINITION",
    "RAILING_INVENTORY_VISIBLE",
    "RAILING_KIND",
    "RAILING_LABEL",
    "RAILING_LIGHT_LEVEL",
    "RAILING_MATERIAL_ID",
    "RAILING_MULTI_BLOCK_OBJECT",
    "RAILING_NEIGHBOUR_CONNECTION_SUPPORTED",
    "RAILING_OPAQUE",
    "RAILING_ORIENTATION_SUPPORTED",
    "RAILING_PERSIST_AS_BLOCK_TYPE",
    "RAILING_PLACEABLE",
    "RAILING_PLACEMENT_COMMAND",
    "RAILING_REMOVAL_COMMAND",
    "RAILING_RENDER_MODE",
    "RAILING_REPLACEABLE",
    "RAILING_RESERVED_CELL_VALUE",
    "RAILING_RUNTIME_BLOCK_TYPE_ID",
    "RAILING_SELECTABLE",
    "RAILING_SHAPE_TYPE",
    "RAILING_SOLID",
    "RAILING_STACK_SIZE",
    "RAILING_STATUS_SCHEMA_VERSION",
    "RAILING_SYSTEM_BLOCK_ID",
    "RAILING_SYSTEM_CATALOG_SCHEMA_VERSION",
    "RAILING_TARGETABLE",
    "RAILING_TEXTURE_ID",
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

_RAILING_DEFINITION_EXPORT_SET: Final[frozenset[str]] = frozenset(
    RAILING_DEFINITION_EXPORTS
)


# -----------------------------------------------------------------------------
# Static type-checking imports
#
# Runtime behavior remains lazy. These imports only help static analyzers and
# IDEs resolve the facade's public API.
# -----------------------------------------------------------------------------

if TYPE_CHECKING:
    from .definition import (
        RAILING_BREAKABLE,
        RAILING_COLLIDABLE,
        RAILING_CURRENT_COLLISION,
        RAILING_CURRENT_GEOMETRY,
        RAILING_DEFAULT_PALETTE_INDEX,
        RAILING_DEFINITION_MODULE_VERSION,
        RAILING_DEFINITION_VERSION,
        RAILING_DESCRIPTION,
        RAILING_EMITS_LIGHT,
        RAILING_FUTURE_GEOMETRY,
        RAILING_HARDNESS,
        RAILING_ICON_ID,
        RAILING_IMMUTABLE_DEFINITION,
        RAILING_INVENTORY_VISIBLE,
        RAILING_KIND,
        RAILING_LABEL,
        RAILING_LIGHT_LEVEL,
        RAILING_MATERIAL_ID,
        RAILING_MULTI_BLOCK_OBJECT,
        RAILING_NEIGHBOUR_CONNECTION_SUPPORTED,
        RAILING_OPAQUE,
        RAILING_ORIENTATION_SUPPORTED,
        RAILING_PERSIST_AS_BLOCK_TYPE,
        RAILING_PLACEABLE,
        RAILING_PLACEMENT_COMMAND,
        RAILING_REMOVAL_COMMAND,
        RAILING_RENDER_MODE,
        RAILING_REPLACEABLE,
        RAILING_RESERVED_CELL_VALUE,
        RAILING_RUNTIME_BLOCK_TYPE_ID,
        RAILING_SELECTABLE,
        RAILING_SHAPE_TYPE,
        RAILING_SOLID,
        RAILING_STACK_SIZE,
        RAILING_STATUS_SCHEMA_VERSION,
        RAILING_SYSTEM_BLOCK_ID,
        RAILING_SYSTEM_CATALOG_SCHEMA_VERSION,
        RAILING_TARGETABLE,
        RAILING_TEXTURE_ID,
        RailingDefinitionError,
        RailingInvariantError,
        RailingSerializationError,
        build_railing_palette_entry,
        build_railing_persistent_values,
        clear_railing_definition_caches,
        collect_railing_invariant_errors,
        compare_railing_block_type,
        get_railing_definition,
        get_railing_definition_debug_summary,
        get_railing_definition_status,
        get_railing_metadata,
        is_railing_block_type_in_sync,
        is_railing_identifier,
        is_railing_runtime_block_type_id,
        is_railing_system_block_id,
        require_railing_definition,
        require_railing_definition_ready,
        serialize_railing_definition,
        serialize_railing_for_system_catalog,
        validate_railing_definition,
    )


# -----------------------------------------------------------------------------
# Exceptions
# -----------------------------------------------------------------------------

class RailingPackageError(RuntimeError):
    """
    Base error for the Railing package facade.
    """


class RailingPackageImportError(RailingPackageError):
    """
    Raised when the Railing definition module cannot be imported.
    """

    def __init__(
        self,
        message: str,
        *,
        attempted_imports: Sequence[str] = (),
        import_errors: Optional[Mapping[str, str]] = None,
    ) -> None:
        self.attempted_imports = tuple(
            str(path).strip()
            for path in attempted_imports
            if str(path).strip()
        )

        self.import_errors = MappingProxyType(
            {
                str(path): str(error)
                for path, error in dict(import_errors or {}).items()
            }
        )

        super().__init__(message)


class RailingPackageExportError(RailingPackageError):
    """
    Raised when a required public export is missing or invalid.
    """

    def __init__(
        self,
        export_name: str,
        message: Optional[str] = None,
    ) -> None:
        self.export_name = str(export_name).strip()

        super().__init__(
            message
            or (
                "The Railing definition module does not provide required "
                f"export '{self.export_name}'."
            )
        )


class RailingPackageNotReadyError(RailingPackageError):
    """
    Raised when package readiness validation fails.
    """

    def __init__(
        self,
        errors: Sequence[Any],
    ) -> None:
        normalized_errors = _normalize_error_messages(errors)

        self.errors = normalized_errors

        details = (
            "; ".join(normalized_errors)
            if normalized_errors
            else "unknown Railing package readiness failure"
        )

        super().__init__(
            f"The built-in Railing package is not ready: {details}"
        )


# -----------------------------------------------------------------------------
# Diagnostic data structures
# -----------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class RailingPackageImportAttempt:
    """
    Result of one possible Railing definition-module import.
    """

    import_path: str
    imported: bool

    error_type: Optional[str] = None
    error: Optional[str] = None

    traceback_text: Optional[str] = field(
        default=None,
        repr=False,
    )

    def to_dict(
        self,
        *,
        include_traceback: bool = False,
    ) -> dict[str, Any]:
        """
        Serialize one import-attempt record.
        """
        return {
            "importPath": self.import_path,
            "imported": self.imported,
            "errorType": self.error_type,
            "error": self.error,
            "traceback": (
                self.traceback_text
                if include_traceback
                else None
            ),
        }


@dataclass(frozen=True, slots=True)
class RailingPackageStatus:
    """
    Complete package and definition readiness status.
    """

    ready: bool

    package_name: str
    package_version: str
    package_source_path: str
    definition_source_path: str

    definition_module_imported: bool
    definition_module_name: Optional[str]
    definition_module_path: Optional[str]

    expected_exports: tuple[str, ...]
    available_exports: tuple[str, ...]
    missing_exports: tuple[str, ...]

    definition_ready: bool
    definition_status: Mapping[str, Any]

    system_block_id: Optional[str]
    runtime_block_type_id: Optional[str]
    definition_version: Optional[str]
    definition_fingerprint: Optional[str]

    persistent_runtime_block: bool
    inventory_visible: bool
    placeable: bool
    breakable: bool
    solid: bool
    collidable: bool

    errors: tuple[str, ...]
    import_attempts: tuple[RailingPackageImportAttempt, ...]

    def to_dict(
        self,
        *,
        include_tracebacks: bool = False,
    ) -> dict[str, Any]:
        """
        Serialize status for APIs, logs and diagnostic tooling.
        """
        return {
            "schemaVersion": (
                RAILING_PACKAGE_STATUS_SCHEMA_VERSION
            ),
            "ready": self.ready,
            "packageName": self.package_name,
            "packageVersion": self.package_version,
            "packageSourcePath": self.package_source_path,
            "definitionSourcePath": self.definition_source_path,
            "definitionModuleImported": (
                self.definition_module_imported
            ),
            "definitionModuleName": self.definition_module_name,
            "definitionModulePath": self.definition_module_path,
            "expectedExports": list(self.expected_exports),
            "availableExports": list(self.available_exports),
            "missingExports": list(self.missing_exports),
            "definitionReady": self.definition_ready,
            "definitionStatus": _make_json_safe(
                self.definition_status
            ),
            "systemBlockId": self.system_block_id,
            "runtimeBlockTypeId": self.runtime_block_type_id,
            "definitionVersion": self.definition_version,
            "definitionFingerprint": self.definition_fingerprint,
            "persistentRuntimeBlock": (
                self.persistent_runtime_block
            ),
            "inventoryVisible": self.inventory_visible,
            "placeable": self.placeable,
            "breakable": self.breakable,
            "solid": self.solid,
            "collidable": self.collidable,
            "errors": list(self.errors),
            "importAttempts": [
                attempt.to_dict(
                    include_traceback=include_tracebacks,
                )
                for attempt in self.import_attempts
            ],
        }


# -----------------------------------------------------------------------------
# Internal mutable diagnostic state
#
# The definition module itself remains immutable. This list only records import
# attempts for diagnostics and can be cleared explicitly in tests.
# -----------------------------------------------------------------------------

_IMPORT_ATTEMPTS: list[RailingPackageImportAttempt] = []


# -----------------------------------------------------------------------------
# Safe primitive helpers
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


def _normalize_error_messages(
    errors: Sequence[Any] | None,
) -> tuple[str, ...]:
    """
    Normalize and deduplicate diagnostic messages while preserving order.
    """
    if errors is None:
        return tuple()

    try:
        values = tuple(errors)
    except Exception:
        values = (errors,)

    normalized: list[str] = []
    seen: set[str] = set()

    for error in values:
        try:
            text = str(error).strip()
        except Exception:
            text = type(error).__name__

        if not text or text in seen:
            continue

        seen.add(text)
        normalized.append(text)

    return tuple(normalized)


def _safe_module_file(
    module: ModuleType | Any,
) -> Optional[str]:
    """
    Return a module source path when available.
    """
    try:
        value = getattr(module, "__file__", None)
    except Exception:
        return None

    if value is None:
        return None

    try:
        text = str(value).strip()
    except Exception:
        return None

    return text or None


def _safe_module_name(
    module: ModuleType | Any,
) -> Optional[str]:
    """
    Return a module's canonical import name when available.
    """
    try:
        value = getattr(module, "__name__", None)
    except Exception:
        return None

    if value is None:
        return None

    try:
        text = str(value).strip()
    except Exception:
        return None

    return text or None


def _safe_mapping_bool(
    mapping: Mapping[str, Any],
    key: str,
    *,
    default: bool = False,
) -> bool:
    """
    Read a bool-like value from a mapping without raising.
    """
    try:
        value = mapping.get(key, default)
    except Exception:
        return bool(default)

    if isinstance(value, bool):
        return value

    if isinstance(value, int):
        return bool(value)

    if value is None:
        return bool(default)

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
        "ready",
    }:
        return True

    if text in {
        "0",
        "false",
        "no",
        "n",
        "off",
        "disabled",
        "not-ready",
    }:
        return False

    return bool(default)


def _safe_mapping_text(
    mapping: Mapping[str, Any],
    key: str,
) -> Optional[str]:
    """
    Read an optional normalized text value from a mapping.
    """
    try:
        value = mapping.get(key)
    except Exception:
        return None

    if value is None:
        return None

    try:
        text = str(value).strip()
    except Exception:
        return None

    return text or None


def _make_json_safe(
    value: Any,
    *,
    _seen: Optional[set[int]] = None,
    _depth: int = 0,
    max_depth: int = 50,
) -> Any:
    """
    Convert package diagnostics into recursion-safe JSON-compatible values.
    """
    if _depth > max_depth:
        return "<max-depth-exceeded>"

    if value is None or isinstance(
        value,
        (str, int, float, bool),
    ):
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
            converted = to_dict()
        except Exception:
            converted = None

        if converted is not None:
            return _make_json_safe(
                converted,
                _seen=_seen,
                _depth=_depth + 1,
                max_depth=max_depth,
            )

    try:
        return str(value)
    except Exception:
        return "<unserializable-value>"


# -----------------------------------------------------------------------------
# Import-path resolution
# -----------------------------------------------------------------------------

@lru_cache(maxsize=1)
def get_railing_definition_import_paths() -> tuple[str, ...]:
    """
    Return supported definition-module import paths in resolution order.

    The active package name is preferred. Explicit fallbacks support selected
    tests and development shells that expose either ``src`` or
    ``system_blocks`` as the top-level import root.
    """
    candidates: list[str] = []

    try:
        current_package = str(__name__).strip()
    except Exception:
        current_package = ""

    if current_package:
        candidates.append(
            f"{current_package}.{RAILING_DEFINITION_MODULE_NAME}"
        )

    candidates.extend(
        [
            "src.system_blocks.railing.definition",
            "system_blocks.railing.definition",
        ]
    )

    resolved: list[str] = []
    seen: set[str] = set()

    for candidate in candidates:
        try:
            normalized = str(candidate).strip()
        except Exception:
            continue

        if not normalized or normalized in seen:
            continue

        seen.add(normalized)
        resolved.append(normalized)

    return tuple(resolved)


def _record_import_attempt(
    attempt: RailingPackageImportAttempt,
) -> None:
    """
    Record one import attempt without allowing diagnostics to cause failure.
    """
    try:
        _IMPORT_ATTEMPTS.append(attempt)
    except Exception:
        pass


@lru_cache(maxsize=1)
def get_railing_definition_module() -> ModuleType:
    """
    Import and return the Railing definition module.

    Successful resolution is cached for the process lifetime. When all
    candidates fail, the resulting exception contains each attempted path and
    its error.
    """
    attempted_paths = get_railing_definition_import_paths()
    import_errors: dict[str, str] = {}

    for import_path in attempted_paths:
        try:
            module = importlib.import_module(import_path)

        except Exception as exc:
            error_text = _safe_exception_text(exc)

            import_errors[import_path] = (
                f"{type(exc).__name__}: {error_text}"
            )

            _record_import_attempt(
                RailingPackageImportAttempt(
                    import_path=import_path,
                    imported=False,
                    error_type=type(exc).__name__,
                    error=error_text,
                    traceback_text=traceback.format_exc(),
                )
            )

            continue

        if not isinstance(module, ModuleType):
            error_text = (
                "importlib returned an object that is not a ModuleType"
            )

            import_errors[import_path] = (
                f"TypeError: {error_text}"
            )

            _record_import_attempt(
                RailingPackageImportAttempt(
                    import_path=import_path,
                    imported=False,
                    error_type="TypeError",
                    error=error_text,
                    traceback_text=None,
                )
            )

            continue

        _record_import_attempt(
            RailingPackageImportAttempt(
                import_path=import_path,
                imported=True,
                error_type=None,
                error=None,
                traceback_text=None,
            )
        )

        return module

    raise RailingPackageImportError(
        "Could not import the built-in Railing definition module. "
        f"Attempted paths: {', '.join(attempted_paths)}.",
        attempted_imports=attempted_paths,
        import_errors=import_errors,
    )


# -----------------------------------------------------------------------------
# Export resolution
# -----------------------------------------------------------------------------

@lru_cache(maxsize=512)
def _resolve_railing_export_cached(
    name: str,
) -> Any:
    """
    Resolve one approved export from the cached definition module.
    """
    normalized_name = str(name).strip()

    if normalized_name not in _RAILING_DEFINITION_EXPORT_SET:
        raise AttributeError(
            f"Module '{__name__}' has no public attribute "
            f"'{normalized_name}'."
        )

    module = get_railing_definition_module()

    try:
        return getattr(module, normalized_name)

    except AttributeError as exc:
        raise RailingPackageExportError(
            normalized_name
        ) from exc

    except Exception as exc:
        raise RailingPackageExportError(
            normalized_name,
            (
                "Could not resolve Railing definition export "
                f"'{normalized_name}': "
                f"{type(exc).__name__}: {_safe_exception_text(exc)}"
            ),
        ) from exc


def get_railing_export(
    name: Any,
) -> Any:
    """
    Resolve one named public Railing export.

    Private definition-module details are intentionally unavailable through this
    facade.
    """
    try:
        normalized_name = str(name).strip()
    except Exception as exc:
        raise AttributeError(
            "Railing export name must be text-like."
        ) from exc

    if not normalized_name:
        raise AttributeError(
            "Railing export name is required."
        )

    return _resolve_railing_export_cached(
        normalized_name
    )


@lru_cache(maxsize=1)
def get_railing_exports() -> Mapping[str, Any]:
    """
    Return an immutable mapping of all required definition exports.

    Calling this function performs a complete export check. It raises when any
    expected symbol is missing.
    """
    resolved: dict[str, Any] = {}

    for export_name in RAILING_DEFINITION_EXPORTS:
        resolved[export_name] = get_railing_export(
            export_name
        )

    return MappingProxyType(resolved)


# -----------------------------------------------------------------------------
# Definition readiness helpers
# -----------------------------------------------------------------------------

def _available_definition_exports(
    module: ModuleType,
) -> tuple[str, ...]:
    """
    Return sorted expected exports currently available on the module.
    """
    available: list[str] = []

    for export_name in RAILING_DEFINITION_EXPORTS:
        try:
            getattr(module, export_name)
        except Exception:
            continue

        available.append(export_name)

    return tuple(sorted(available))


def _extract_status_errors(
    status: Mapping[str, Any],
) -> tuple[str, ...]:
    """
    Extract normalized errors from a definition status mapping.
    """
    errors: list[Any] = []

    try:
        raw_errors = status.get("errors")
    except Exception:
        raw_errors = None

    if isinstance(
        raw_errors,
        (list, tuple, set, frozenset),
    ):
        errors.extend(raw_errors)

    elif raw_errors:
        errors.append(raw_errors)

    try:
        raw_error = status.get("error")
    except Exception:
        raw_error = None

    if raw_error:
        errors.append(raw_error)

    return _normalize_error_messages(errors)


def _read_railing_definition_status(
    module: ModuleType,
) -> tuple[
    bool,
    Mapping[str, Any],
    tuple[str, ...],
]:
    """
    Read definition-level status without allowing diagnostics to crash.
    """
    try:
        status_factory = getattr(
            module,
            "get_railing_definition_status",
        )
    except Exception as exc:
        return (
            False,
            MappingProxyType({}),
            (
                "Railing definition module does not expose "
                "get_railing_definition_status(): "
                f"{type(exc).__name__}: "
                f"{_safe_exception_text(exc)}",
            ),
        )

    if not callable(status_factory):
        return (
            False,
            MappingProxyType({}),
            (
                "get_railing_definition_status is not callable.",
            ),
        )

    try:
        raw_status = status_factory()

    except Exception as exc:
        return (
            False,
            MappingProxyType({}),
            (
                "Calling get_railing_definition_status() failed: "
                f"{type(exc).__name__}: "
                f"{_safe_exception_text(exc)}",
            ),
        )

    if isinstance(raw_status, Mapping):
        status = dict(raw_status)

    else:
        status = {
            "ready": False,
            "errors": [
                "Railing definition status did not return a mapping."
            ],
            "rawStatus": _make_json_safe(raw_status),
        }

    ready = _safe_mapping_bool(
        status,
        "ready",
        default=False,
    )

    errors = list(_extract_status_errors(status))

    if not ready and not errors:
        errors.append(
            "Railing definition status reports ready=false."
        )

    return (
        ready,
        MappingProxyType(status),
        _normalize_error_messages(errors),
    )


def _build_failed_package_status(
    *,
    errors: Sequence[Any],
    import_attempts: Optional[
        Sequence[RailingPackageImportAttempt]
    ] = None,
) -> RailingPackageStatus:
    """
    Build a complete failed status with stable defaults.
    """
    return RailingPackageStatus(
        ready=False,
        package_name=RAILING_PACKAGE_NAME,
        package_version=RAILING_PACKAGE_VERSION,
        package_source_path=RAILING_PACKAGE_SOURCE_PATH,
        definition_source_path=RAILING_DEFINITION_SOURCE_PATH,
        definition_module_imported=False,
        definition_module_name=None,
        definition_module_path=None,
        expected_exports=RAILING_DEFINITION_EXPORTS,
        available_exports=tuple(),
        missing_exports=RAILING_DEFINITION_EXPORTS,
        definition_ready=False,
        definition_status=MappingProxyType({}),
        system_block_id=None,
        runtime_block_type_id=None,
        definition_version=None,
        definition_fingerprint=None,
        persistent_runtime_block=False,
        inventory_visible=False,
        placeable=False,
        breakable=False,
        solid=False,
        collidable=False,
        errors=_normalize_error_messages(errors),
        import_attempts=tuple(
            import_attempts
            if import_attempts is not None
            else _IMPORT_ATTEMPTS
        ),
    )


@lru_cache(maxsize=1)
def get_railing_package_status() -> RailingPackageStatus:
    """
    Return cached package and canonical-definition readiness diagnostics.
    """
    errors: list[Any] = []

    try:
        module = get_railing_definition_module()

    except Exception as exc:
        return _build_failed_package_status(
            errors=(
                "Could not import Railing definition module: "
                f"{type(exc).__name__}: "
                f"{_safe_exception_text(exc)}",
            )
        )

    available_exports = _available_definition_exports(
        module
    )

    available_export_set = set(available_exports)

    missing_exports = tuple(
        export_name
        for export_name in RAILING_DEFINITION_EXPORTS
        if export_name not in available_export_set
    )

    if missing_exports:
        errors.append(
            "Railing definition module is missing exports: "
            + ", ".join(missing_exports)
        )

    (
        definition_ready,
        definition_status,
        definition_errors,
    ) = _read_railing_definition_status(module)

    errors.extend(definition_errors)

    system_block_id = _safe_mapping_text(
        definition_status,
        "systemBlockId",
    )

    runtime_block_type_id = _safe_mapping_text(
        definition_status,
        "runtimeBlockTypeId",
    )

    definition_version = _safe_mapping_text(
        definition_status,
        "definitionVersion",
    )

    definition_fingerprint = _safe_mapping_text(
        definition_status,
        "definitionFingerprint",
    )

    persist_as_block_type = _safe_mapping_bool(
        definition_status,
        "persistAsBlockType",
        default=False,
    )

    inventory_visible = _safe_mapping_bool(
        definition_status,
        "inventoryVisible",
        default=False,
    )

    placeable = _safe_mapping_bool(
        definition_status,
        "placeable",
        default=False,
    )

    breakable = _safe_mapping_bool(
        definition_status,
        "breakable",
        default=False,
    )

    solid = _safe_mapping_bool(
        definition_status,
        "solid",
        default=False,
    )

    collidable = _safe_mapping_bool(
        definition_status,
        "collidable",
        default=False,
    )

    # Package-level invariants are intentionally repeated here. This makes an
    # incomplete or accidentally weakened definition status visible even when
    # the definition module reports ready=true.
    if definition_ready:
        if not system_block_id:
            errors.append(
                "Ready Railing definition has no systemBlockId."
            )

        if not runtime_block_type_id:
            errors.append(
                "Ready Railing definition has no runtimeBlockTypeId."
            )

        if not definition_version:
            errors.append(
                "Ready Railing definition has no definitionVersion."
            )

        if not definition_fingerprint:
            errors.append(
                "Ready Railing definition has no definitionFingerprint."
            )

        if not persist_as_block_type:
            errors.append(
                "Ready Railing definition must persist as BlockType."
            )

        if not inventory_visible:
            errors.append(
                "Ready Railing definition must be inventory-visible."
            )

        if not placeable:
            errors.append(
                "Ready Railing definition must be placeable."
            )

        if not breakable:
            errors.append(
                "Ready Railing definition must be breakable."
            )

        if not solid:
            errors.append(
                "Ready Railing definition must be solid."
            )

        if not collidable:
            errors.append(
                "Ready Railing definition must be collidable."
            )

    normalized_errors = _normalize_error_messages(
        errors
    )

    ready = bool(
        module is not None
        and not missing_exports
        and definition_ready
        and persist_as_block_type
        and inventory_visible
        and placeable
        and breakable
        and solid
        and collidable
        and bool(system_block_id)
        and bool(runtime_block_type_id)
        and bool(definition_version)
        and bool(definition_fingerprint)
        and not normalized_errors
    )

    return RailingPackageStatus(
        ready=ready,
        package_name=RAILING_PACKAGE_NAME,
        package_version=RAILING_PACKAGE_VERSION,
        package_source_path=RAILING_PACKAGE_SOURCE_PATH,
        definition_source_path=RAILING_DEFINITION_SOURCE_PATH,
        definition_module_imported=True,
        definition_module_name=_safe_module_name(module),
        definition_module_path=_safe_module_file(module),
        expected_exports=RAILING_DEFINITION_EXPORTS,
        available_exports=available_exports,
        missing_exports=missing_exports,
        definition_ready=definition_ready,
        definition_status=definition_status,
        system_block_id=system_block_id,
        runtime_block_type_id=runtime_block_type_id,
        definition_version=definition_version,
        definition_fingerprint=definition_fingerprint,
        persistent_runtime_block=persist_as_block_type,
        inventory_visible=inventory_visible,
        placeable=placeable,
        breakable=breakable,
        solid=solid,
        collidable=collidable,
        errors=normalized_errors,
        import_attempts=tuple(_IMPORT_ATTEMPTS),
    )


def is_railing_package_ready() -> bool:
    """
    Return whether the package and canonical definition are ready.
    """
    try:
        return bool(
            get_railing_package_status().ready
        )
    except Exception:
        return False


def require_railing_package_ready() -> ModuleType:
    """
    Return the definition module after package readiness validation.
    """
    status = get_railing_package_status()

    if not status.ready:
        raise RailingPackageNotReadyError(
            status.errors
            or (
                "Railing package readiness check failed.",
            )
        )

    return get_railing_definition_module()


def require_railing_exports_ready() -> Mapping[str, Any]:
    """
    Return every definition export after package readiness validation.
    """
    require_railing_package_ready()

    try:
        return get_railing_exports()

    except RailingPackageError:
        raise

    except Exception as exc:
        raise RailingPackageNotReadyError(
            (
                "Could not resolve all required Railing exports: "
                f"{type(exc).__name__}: "
                f"{_safe_exception_text(exc)}",
            )
        ) from exc


def get_railing_package_debug_summary(
    *,
    include_tracebacks: bool = False,
) -> dict[str, Any]:
    """
    Return a non-raising JSON-safe package diagnostic summary.
    """
    try:
        return get_railing_package_status().to_dict(
            include_tracebacks=include_tracebacks,
        )

    except Exception as exc:
        return {
            "schemaVersion": (
                RAILING_PACKAGE_STATUS_SCHEMA_VERSION
            ),
            "ready": False,
            "packageName": RAILING_PACKAGE_NAME,
            "packageVersion": RAILING_PACKAGE_VERSION,
            "packageSourcePath": RAILING_PACKAGE_SOURCE_PATH,
            "definitionSourcePath": (
                RAILING_DEFINITION_SOURCE_PATH
            ),
            "errors": [
                "Could not build Railing package diagnostics."
            ],
            "errorType": type(exc).__name__,
            "error": _safe_exception_text(exc),
        }


# -----------------------------------------------------------------------------
# Lazy package attribute protocol
# -----------------------------------------------------------------------------

def __getattr__(
    name: str,
) -> Any:
    """
    Lazily expose approved symbols from ``definition.py``.

    Resolved attributes are cached directly in this package's globals so future
    access bypasses both ``__getattr__`` and the export resolver.
    """
    if name in _RAILING_DEFINITION_EXPORT_SET:
        value = get_railing_export(name)

        try:
            globals()[name] = value
        except Exception:
            pass

        return value

    raise AttributeError(
        f"Module '{__name__}' has no attribute '{name}'."
    )


def __dir__() -> list[str]:
    """
    Return stable discoverable package attributes.
    """
    names = set(globals().keys())
    names.update(RAILING_DEFINITION_EXPORTS)
    return sorted(names)


# -----------------------------------------------------------------------------
# Cache management
# -----------------------------------------------------------------------------

def clear_railing_package_caches(
    *,
    clear_definition_caches: bool = True,
    clear_import_attempts: bool = True,
) -> None:
    """
    Clear package facade and optional definition caches.

    Intended uses:

    - unit tests,
    - explicit diagnostic refresh,
    - development reload tooling.

    Normal production request paths should not call this function.
    """
    module: Optional[ModuleType] = None

    if clear_definition_caches:
        try:
            module = get_railing_definition_module()
        except Exception:
            module = None

        if module is not None:
            try:
                clear_function = getattr(
                    module,
                    "clear_railing_definition_caches",
                    None,
                )

                if callable(clear_function):
                    clear_function()

            except Exception:
                pass

    # Remove lazily cached delegated attributes from package globals.
    for export_name in RAILING_DEFINITION_EXPORTS:
        try:
            globals().pop(export_name, None)
        except Exception:
            pass

    _resolve_railing_export_cached.cache_clear()
    get_railing_exports.cache_clear()
    get_railing_definition_module.cache_clear()
    get_railing_definition_import_paths.cache_clear()
    get_railing_package_status.cache_clear()

    if clear_import_attempts:
        try:
            _IMPORT_ATTEMPTS.clear()
        except Exception:
            pass


# -----------------------------------------------------------------------------
# Public exports
# -----------------------------------------------------------------------------

__all__ = [
    "RAILING_DEFINITION_EXPORTS",
    "RAILING_DEFINITION_MODULE_NAME",
    "RAILING_DEFINITION_SOURCE_PATH",
    "RAILING_PACKAGE_NAME",
    "RAILING_PACKAGE_SOURCE_PATH",
    "RAILING_PACKAGE_STATUS_SCHEMA_VERSION",
    "RAILING_PACKAGE_VERSION",
    "RailingPackageError",
    "RailingPackageExportError",
    "RailingPackageImportAttempt",
    "RailingPackageImportError",
    "RailingPackageNotReadyError",
    "RailingPackageStatus",
    "clear_railing_package_caches",
    "get_railing_definition_import_paths",
    "get_railing_definition_module",
    "get_railing_export",
    "get_railing_exports",
    "get_railing_package_debug_summary",
    "get_railing_package_status",
    "is_railing_package_ready",
    "require_railing_exports_ready",
    "require_railing_package_ready",
    *RAILING_DEFINITION_EXPORTS,
]