# services/vectoplan-chunk/src/system_blocks/air/__init__.py
"""
Public package facade for the built-in VECTOPLAN Air definition.

This package exposes the canonical Air definition implemented in:

    src/system_blocks/air/definition.py

Air is a reserved world-cell state with the following invariant:

    cellValue = 0

Air is intentionally not:

- a persistent BlockType row,
- a positive chunk palette entry,
- an editor inventory item,
- a selectable or placeable runtime block.

The package facade is deliberately lazy. Importing ``src.system_blocks.air``
does not immediately construct the Air definition. The definition module is
loaded only when one of its exported attributes or helper functions is first
used.

This provides several advantages:

- reduced import-time coupling,
- controlled error reporting,
- compatibility with status and diagnostic endpoints,
- easier unit testing,
- safer application startup,
- no database or Flask side effects,
- cached module resolution for repeated calls.

The package contains no SQLAlchemy, Flask, database, route or bootstrap logic.
"""

from __future__ import annotations

import importlib
import traceback
from dataclasses import dataclass, field
from functools import lru_cache
from types import MappingProxyType, ModuleType
from typing import Any, Final, Mapping, Optional, TYPE_CHECKING


# -----------------------------------------------------------------------------
# Static package metadata
# -----------------------------------------------------------------------------

AIR_PACKAGE_NAME: Final[str] = "src.system_blocks.air"
AIR_PACKAGE_VERSION: Final[str] = "1.0.0"
AIR_DEFINITION_MODULE_NAME: Final[str] = "definition"

AIR_PACKAGE_STATUS_SCHEMA_VERSION: Final[str] = (
    "system-block-air-package-status.schema.v1"
)

AIR_PACKAGE_SOURCE_PATH: Final[str] = (
    "services/vectoplan-chunk/src/system_blocks/air"
)

AIR_DEFINITION_SOURCE_PATH: Final[str] = (
    "services/vectoplan-chunk/src/system_blocks/air/definition.py"
)


# -----------------------------------------------------------------------------
# Public definition-module exports
#
# Keep this list synchronized with definition.py.__all__. The package validates
# these exports through get_air_package_status(), so missing or accidentally
# renamed public symbols become visible in diagnostics.
# -----------------------------------------------------------------------------

AIR_DEFINITION_EXPORTS: Final[tuple[str, ...]] = (
    "AIR_BREAKABLE",
    "AIR_COLLIDABLE",
    "AIR_CREATION_COMMAND",
    "AIR_DEFINITION_MODULE_VERSION",
    "AIR_DEFINITION_VERSION",
    "AIR_DESCRIPTION",
    "AIR_EMITS_LIGHT",
    "AIR_FORBIDDEN_PLACEMENT_COMMAND",
    "AIR_HARDNESS",
    "AIR_IMMUTABLE_DEFINITION",
    "AIR_INVENTORY_VISIBLE",
    "AIR_KIND",
    "AIR_LABEL",
    "AIR_LIGHT_LEVEL",
    "AIR_OPAQUE",
    "AIR_PERSIST_AS_BLOCK_TYPE",
    "AIR_PLACEABLE",
    "AIR_RENDER_MODE",
    "AIR_REPLACEABLE",
    "AIR_RESERVED_CELL_VALUE",
    "AIR_RUNTIME_BLOCK_TYPE_ID",
    "AIR_SELECTABLE",
    "AIR_SET_BLOCK_ERROR_CODE",
    "AIR_SHAPE_TYPE",
    "AIR_SOLID",
    "AIR_STACK_SIZE",
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

_AIR_DEFINITION_EXPORT_SET: Final[frozenset[str]] = frozenset(
    AIR_DEFINITION_EXPORTS
)


# -----------------------------------------------------------------------------
# Optional type-checking imports
#
# Runtime loading remains lazy. Static analyzers can still resolve the facade's
# public API through these imports.
# -----------------------------------------------------------------------------

if TYPE_CHECKING:
    from .definition import (
        AIR_BREAKABLE,
        AIR_COLLIDABLE,
        AIR_CREATION_COMMAND,
        AIR_DEFINITION_MODULE_VERSION,
        AIR_DEFINITION_VERSION,
        AIR_DESCRIPTION,
        AIR_EMITS_LIGHT,
        AIR_FORBIDDEN_PLACEMENT_COMMAND,
        AIR_HARDNESS,
        AIR_IMMUTABLE_DEFINITION,
        AIR_INVENTORY_VISIBLE,
        AIR_KIND,
        AIR_LABEL,
        AIR_LIGHT_LEVEL,
        AIR_OPAQUE,
        AIR_PERSIST_AS_BLOCK_TYPE,
        AIR_PLACEABLE,
        AIR_RENDER_MODE,
        AIR_REPLACEABLE,
        AIR_RESERVED_CELL_VALUE,
        AIR_RUNTIME_BLOCK_TYPE_ID,
        AIR_SELECTABLE,
        AIR_SET_BLOCK_ERROR_CODE,
        AIR_SHAPE_TYPE,
        AIR_SOLID,
        AIR_STACK_SIZE,
        AIR_SYSTEM_BLOCK_ID,
        AIR_TARGETABLE,
        AirDefinitionError,
        AirInvariantError,
        clear_air_definition_caches,
        collect_air_invariant_errors,
        get_air_definition,
        get_air_definition_status,
        get_air_metadata,
        is_air_cell_value,
        is_air_runtime_block_type_id,
        is_air_system_block_id,
        is_forbidden_air_set_block_id,
        require_air_definition,
        require_air_definition_ready,
        serialize_air_definition,
        serialize_air_for_world_blocks_route,
        validate_air_definition,
    )


# -----------------------------------------------------------------------------
# Exceptions
# -----------------------------------------------------------------------------

class AirPackageError(RuntimeError):
    """
    Base error for failures in the Air package facade.
    """


class AirPackageImportError(AirPackageError):
    """
    Raised when the Air definition module cannot be imported.
    """

    def __init__(
        self,
        message: str,
        *,
        attempted_imports: tuple[str, ...] = (),
        import_errors: Mapping[str, str] | None = None,
    ) -> None:
        self.attempted_imports = tuple(attempted_imports)
        self.import_errors = MappingProxyType(
            dict(import_errors or {})
        )

        super().__init__(message)


class AirPackageNotReadyError(AirPackageError):
    """
    Raised when the Air package or its definition is not ready.
    """

    def __init__(
        self,
        errors: tuple[str, ...] | list[str],
    ) -> None:
        normalized_errors = tuple(
            str(error).strip()
            for error in errors
            if str(error).strip()
        )

        self.errors = normalized_errors

        details = (
            "; ".join(normalized_errors)
            if normalized_errors
            else "unknown Air package readiness failure"
        )

        super().__init__(
            f"The built-in Air package is not ready: {details}"
        )


# -----------------------------------------------------------------------------
# Diagnostic data structures
# -----------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class AirPackageImportAttempt:
    """
    Result of one possible Air definition-module import.
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
        Serialize one import attempt.
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
class AirPackageStatus:
    """
    Complete readiness status for the Air package.
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

    errors: tuple[str, ...]
    import_attempts: tuple[AirPackageImportAttempt, ...]

    def to_dict(
        self,
        *,
        include_tracebacks: bool = False,
    ) -> dict[str, Any]:
        """
        Serialize package status for APIs, logs and diagnostics.
        """
        return {
            "schemaVersion": AIR_PACKAGE_STATUS_SCHEMA_VERSION,
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
            "errors": list(self.errors),
            "importAttempts": [
                attempt.to_dict(
                    include_traceback=include_tracebacks,
                )
                for attempt in self.import_attempts
            ],
        }


# -----------------------------------------------------------------------------
# Internal state
# -----------------------------------------------------------------------------

_IMPORT_ATTEMPTS: list[AirPackageImportAttempt] = []


# -----------------------------------------------------------------------------
# Safe primitive helpers
# -----------------------------------------------------------------------------

def _safe_exception_text(error: BaseException | Any) -> str:
    """
    Return a robust exception message.
    """
    try:
        text = str(error).strip()
    except Exception:
        text = ""

    return text or type(error).__name__


def _safe_module_file(module: ModuleType | Any) -> Optional[str]:
    """
    Return the source file path of a module when available.
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


def _safe_module_name(module: ModuleType | Any) -> Optional[str]:
    """
    Return the canonical module name when available.
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


def _make_json_safe(
    value: Any,
    *,
    _seen: Optional[set[int]] = None,
    _depth: int = 0,
    max_depth: int = 40,
) -> Any:
    """
    Convert package diagnostics into recursion-safe JSON values.
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


# -----------------------------------------------------------------------------
# Definition-module import resolution
# -----------------------------------------------------------------------------

@lru_cache(maxsize=1)
def get_air_definition_import_paths() -> tuple[str, ...]:
    """
    Return candidate import paths in resolution order.

    The canonical path is derived from the active package name. The explicit
    fallback supports development/test environments that expose ``src`` as a
    top-level package.
    """
    candidates: list[str] = []

    current_package = str(__name__).strip()

    if current_package:
        candidates.append(
            f"{current_package}.{AIR_DEFINITION_MODULE_NAME}"
        )

    candidates.extend(
        [
            "src.system_blocks.air.definition",
            "system_blocks.air.definition",
        ]
    )

    unique_candidates: list[str] = []
    seen: set[str] = set()

    for candidate in candidates:
        normalized = str(candidate).strip()

        if not normalized or normalized in seen:
            continue

        seen.add(normalized)
        unique_candidates.append(normalized)

    return tuple(unique_candidates)


def _record_import_attempt(
    attempt: AirPackageImportAttempt,
) -> None:
    """
    Store one module-import attempt without allowing diagnostics to fail.
    """
    try:
        _IMPORT_ATTEMPTS.append(attempt)
    except Exception:
        pass


@lru_cache(maxsize=1)
def get_air_definition_module() -> ModuleType:
    """
    Import and return the Air definition module.

    The resolved module is cached for the process lifetime. Import failures are
    retained as diagnostic records and reported together after all supported
    paths have been attempted.
    """
    attempted_paths = get_air_definition_import_paths()
    errors: dict[str, str] = {}

    for import_path in attempted_paths:
        try:
            module = importlib.import_module(import_path)
        except Exception as exc:
            error_text = _safe_exception_text(exc)
            errors[import_path] = (
                f"{type(exc).__name__}: {error_text}"
            )

            _record_import_attempt(
                AirPackageImportAttempt(
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
                "importlib returned an object that is not a module"
            )
            errors[import_path] = error_text

            _record_import_attempt(
                AirPackageImportAttempt(
                    import_path=import_path,
                    imported=False,
                    error_type="TypeError",
                    error=error_text,
                    traceback_text=None,
                )
            )
            continue

        _record_import_attempt(
            AirPackageImportAttempt(
                import_path=import_path,
                imported=True,
                error_type=None,
                error=None,
                traceback_text=None,
            )
        )

        return module

    raise AirPackageImportError(
        "Could not import the built-in Air definition module. "
        f"Attempted paths: {', '.join(attempted_paths)}.",
        attempted_imports=attempted_paths,
        import_errors=errors,
    )


# -----------------------------------------------------------------------------
# Export resolution
# -----------------------------------------------------------------------------

@lru_cache(maxsize=256)
def _resolve_air_export_cached(name: str) -> Any:
    """
    Resolve one public export from the cached definition module.
    """
    normalized_name = str(name).strip()

    if normalized_name not in _AIR_DEFINITION_EXPORT_SET:
        raise AttributeError(
            f"Module '{__name__}' has no public attribute "
            f"'{normalized_name}'."
        )

    module = get_air_definition_module()

    try:
        value = getattr(module, normalized_name)
    except AttributeError as exc:
        raise AirPackageNotReadyError(
            (
                "The Air definition module is missing required export "
                f"'{normalized_name}'.",
            )
        ) from exc
    except Exception as exc:
        raise AirPackageError(
            "Could not resolve Air definition export "
            f"'{normalized_name}'."
        ) from exc

    return value


def get_air_export(name: str) -> Any:
    """
    Resolve one named Air definition export.

    Only names declared in ``AIR_DEFINITION_EXPORTS`` are accessible.
    """
    try:
        normalized_name = str(name).strip()
    except Exception as exc:
        raise AttributeError(
            "Air export name must be text-like."
        ) from exc

    return _resolve_air_export_cached(normalized_name)


def get_air_exports() -> Mapping[str, Any]:
    """
    Return an immutable mapping of all public Air definition exports.

    Resolving this mapping imports the definition module and validates that all
    expected symbols exist.
    """
    resolved: dict[str, Any] = {}

    for export_name in AIR_DEFINITION_EXPORTS:
        resolved[export_name] = get_air_export(export_name)

    return MappingProxyType(resolved)


# -----------------------------------------------------------------------------
# Package readiness
# -----------------------------------------------------------------------------

def _available_module_exports(
    module: ModuleType,
) -> tuple[str, ...]:
    """
    Return sorted public exports available on the definition module.
    """
    available: list[str] = []

    for export_name in AIR_DEFINITION_EXPORTS:
        try:
            getattr(module, export_name)
        except Exception:
            continue

        available.append(export_name)

    return tuple(sorted(available))


def _read_definition_status(
    module: ModuleType,
) -> tuple[bool, Mapping[str, Any], tuple[str, ...]]:
    """
    Read the definition-level status without allowing diagnostics to crash.
    """
    errors: list[str] = []

    try:
        status_factory = getattr(
            module,
            "get_air_definition_status",
        )
    except Exception as exc:
        return (
            False,
            MappingProxyType({}),
            (
                "Air definition module does not expose "
                "get_air_definition_status(): "
                f"{type(exc).__name__}: {_safe_exception_text(exc)}",
            ),
        )

    if not callable(status_factory):
        return (
            False,
            MappingProxyType({}),
            (
                "get_air_definition_status is not callable.",
            ),
        )

    try:
        raw_status = status_factory()
    except Exception as exc:
        return (
            False,
            MappingProxyType({}),
            (
                "Calling get_air_definition_status() failed: "
                f"{type(exc).__name__}: {_safe_exception_text(exc)}",
            ),
        )

    if isinstance(raw_status, Mapping):
        status = dict(raw_status)
    else:
        status = {
            "ready": False,
            "error": (
                "Definition status did not return a mapping."
            ),
            "rawStatus": _make_json_safe(raw_status),
        }

        errors.append(
            "Air definition status must be a mapping."
        )

    try:
        definition_ready = bool(status.get("ready", False))
    except Exception:
        definition_ready = False

    if not definition_ready:
        status_errors = status.get("errors")

        if isinstance(
            status_errors,
            (list, tuple, set, frozenset),
        ):
            for error in status_errors:
                text = str(error).strip()

                if text:
                    errors.append(text)

        status_error = status.get("error")

        if status_error:
            errors.append(str(status_error).strip())

        if not errors:
            errors.append(
                "Air definition status reports ready=false."
            )

    return (
        definition_ready,
        MappingProxyType(status),
        tuple(errors),
    )


@lru_cache(maxsize=1)
def get_air_package_status() -> AirPackageStatus:
    """
    Return complete cached readiness diagnostics for the Air package.
    """
    errors: list[str] = []

    try:
        module = get_air_definition_module()
    except Exception as exc:
        errors.append(
            "Could not import Air definition module: "
            f"{type(exc).__name__}: {_safe_exception_text(exc)}"
        )

        return AirPackageStatus(
            ready=False,
            package_name=AIR_PACKAGE_NAME,
            package_version=AIR_PACKAGE_VERSION,
            package_source_path=AIR_PACKAGE_SOURCE_PATH,
            definition_source_path=AIR_DEFINITION_SOURCE_PATH,
            definition_module_imported=False,
            definition_module_name=None,
            definition_module_path=None,
            expected_exports=AIR_DEFINITION_EXPORTS,
            available_exports=tuple(),
            missing_exports=AIR_DEFINITION_EXPORTS,
            definition_ready=False,
            definition_status=MappingProxyType({}),
            errors=tuple(errors),
            import_attempts=tuple(_IMPORT_ATTEMPTS),
        )

    available_exports = _available_module_exports(module)

    available_set = set(available_exports)

    missing_exports = tuple(
        export_name
        for export_name in AIR_DEFINITION_EXPORTS
        if export_name not in available_set
    )

    if missing_exports:
        errors.append(
            "Air definition module is missing exports: "
            + ", ".join(missing_exports)
        )

    (
        definition_ready,
        definition_status,
        definition_errors,
    ) = _read_definition_status(module)

    errors.extend(definition_errors)

    normalized_errors: list[str] = []
    seen_errors: set[str] = set()

    for error in errors:
        text = str(error).strip()

        if not text or text in seen_errors:
            continue

        seen_errors.add(text)
        normalized_errors.append(text)

    ready = bool(
        module is not None
        and not missing_exports
        and definition_ready
        and not normalized_errors
    )

    return AirPackageStatus(
        ready=ready,
        package_name=AIR_PACKAGE_NAME,
        package_version=AIR_PACKAGE_VERSION,
        package_source_path=AIR_PACKAGE_SOURCE_PATH,
        definition_source_path=AIR_DEFINITION_SOURCE_PATH,
        definition_module_imported=True,
        definition_module_name=_safe_module_name(module),
        definition_module_path=_safe_module_file(module),
        expected_exports=AIR_DEFINITION_EXPORTS,
        available_exports=available_exports,
        missing_exports=missing_exports,
        definition_ready=definition_ready,
        definition_status=definition_status,
        errors=tuple(normalized_errors),
        import_attempts=tuple(_IMPORT_ATTEMPTS),
    )


def is_air_package_ready() -> bool:
    """
    Return whether the Air package and canonical definition are ready.
    """
    try:
        return get_air_package_status().ready
    except Exception:
        return False


def require_air_package_ready() -> ModuleType:
    """
    Return the Air definition module or raise when readiness checks fail.
    """
    status = get_air_package_status()

    if not status.ready:
        raise AirPackageNotReadyError(
            status.errors or (
                "Air package readiness check failed.",
            )
        )

    return get_air_definition_module()


def get_air_package_debug_summary(
    *,
    include_tracebacks: bool = False,
) -> dict[str, Any]:
    """
    Return JSON-safe package diagnostics.
    """
    try:
        status = get_air_package_status()

        return status.to_dict(
            include_tracebacks=include_tracebacks,
        )
    except Exception as exc:
        return {
            "schemaVersion": (
                AIR_PACKAGE_STATUS_SCHEMA_VERSION
            ),
            "ready": False,
            "packageName": AIR_PACKAGE_NAME,
            "packageVersion": AIR_PACKAGE_VERSION,
            "packageSourcePath": AIR_PACKAGE_SOURCE_PATH,
            "definitionSourcePath": (
                AIR_DEFINITION_SOURCE_PATH
            ),
            "errors": [
                "Could not build Air package diagnostics."
            ],
            "errorType": type(exc).__name__,
            "error": _safe_exception_text(exc),
        }


# -----------------------------------------------------------------------------
# Lazy package attribute protocol
# -----------------------------------------------------------------------------

def __getattr__(name: str) -> Any:
    """
    Lazily expose public symbols from ``definition.py``.

    Python calls this function when an attribute does not already exist in this
    package module. Restricting delegation to the explicit export set prevents
    private implementation details from leaking through the facade.
    """
    if name in _AIR_DEFINITION_EXPORT_SET:
        value = get_air_export(name)

        # Cache the resolved attribute directly on the package module. Future
        # accesses then bypass both __getattr__ and the export resolver.
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
    standard_names = set(globals().keys())
    standard_names.update(AIR_DEFINITION_EXPORTS)
    return sorted(standard_names)


# -----------------------------------------------------------------------------
# Cache management
# -----------------------------------------------------------------------------

def clear_air_package_caches(
    *,
    clear_definition_caches: bool = True,
    clear_import_attempts: bool = True,
) -> None:
    """
    Clear Air package caches.

    This is intended for tests, development reload tooling and explicit
    diagnostic refreshes. Production request paths should not normally call it.

    When ``clear_definition_caches`` is true, the definition module's own cache
    clear function is invoked before the local module cache is reset.
    """
    module: Optional[ModuleType] = None

    if clear_definition_caches:
        try:
            module = get_air_definition_module()
        except Exception:
            module = None

        if module is not None:
            try:
                clear_function = getattr(
                    module,
                    "clear_air_definition_caches",
                    None,
                )

                if callable(clear_function):
                    clear_function()
            except Exception:
                pass

    for export_name in AIR_DEFINITION_EXPORTS:
        try:
            globals().pop(export_name, None)
        except Exception:
            pass

    _resolve_air_export_cached.cache_clear()
    get_air_definition_module.cache_clear()
    get_air_definition_import_paths.cache_clear()
    get_air_package_status.cache_clear()

    if clear_import_attempts:
        try:
            _IMPORT_ATTEMPTS.clear()
        except Exception:
            pass


# -----------------------------------------------------------------------------
# Public exports
# -----------------------------------------------------------------------------

__all__ = [
    "AIR_DEFINITION_EXPORTS",
    "AIR_DEFINITION_MODULE_NAME",
    "AIR_DEFINITION_SOURCE_PATH",
    "AIR_PACKAGE_NAME",
    "AIR_PACKAGE_SOURCE_PATH",
    "AIR_PACKAGE_STATUS_SCHEMA_VERSION",
    "AIR_PACKAGE_VERSION",
    "AirPackageError",
    "AirPackageImportAttempt",
    "AirPackageImportError",
    "AirPackageNotReadyError",
    "AirPackageStatus",
    "clear_air_package_caches",
    "get_air_definition_import_paths",
    "get_air_definition_module",
    "get_air_export",
    "get_air_exports",
    "get_air_package_debug_summary",
    "get_air_package_status",
    "is_air_package_ready",
    "require_air_package_ready",
    *AIR_DEFINITION_EXPORTS,
]