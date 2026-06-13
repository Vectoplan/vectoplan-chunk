# services/vectoplan-chunk/models/__init__.py
"""
Model registration package for vectoplan-chunk.

This package imports and exposes all SQLAlchemy models used by the chunk
service. Importing this package should register every model class with the
Flask-SQLAlchemy metadata so migrations and `db.create_all()` can see the full
schema.

Current persistent model groups:

    Project / Universe / WorldInstance
    BlockRegistry / BlockType
    ChunkSnapshot
    WorldCommandLog / ChunkEvent
    WorldObjectInstance / WorldObjectChunkRef

Important design rules:
- Import order is intentional.
- Public/API ids are not internal database ids.
- `world_id` is only unique inside a universe.
- ChunkSnapshot uniqueness uses internal `world_db_id + chunk_x/y/z`.
- ChunkEvent is append-only historical truth.
- ChunkSnapshot is current load-truth.
- WorldObjectInstance prepares future multi-block object support.
"""

from __future__ import annotations

import importlib
import traceback
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple, Type


PACKAGE_NAME = "models"

MODEL_IMPORT_ORDER: Tuple[str, ...] = (
    "project",
    "universe",
    "world",
    "block",
    "chunk",
    "event",
    "object",
)

EXPECTED_MODEL_CLASSES: Tuple[str, ...] = (
    "Project",
    "Universe",
    "WorldInstance",
    "BlockRegistry",
    "BlockType",
    "ChunkSnapshot",
    "WorldCommandLog",
    "ChunkEvent",
    "WorldObjectInstance",
    "WorldObjectChunkRef",
)

MODEL_CLASS_TO_MODULE: Dict[str, str] = {
    "Project": "project",
    "Universe": "universe",
    "WorldInstance": "world",
    "BlockRegistry": "block",
    "BlockType": "block",
    "ChunkSnapshot": "chunk",
    "WorldCommandLog": "event",
    "ChunkEvent": "event",
    "WorldObjectInstance": "object",
    "WorldObjectChunkRef": "object",
}


@dataclass(frozen=True)
class ModelImportRecord:
    """Diagnostic record for one model module import."""

    module_name: str
    import_path: str
    imported: bool
    error: Optional[str] = None
    traceback_text: Optional[str] = None
    exported_symbols: Tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class ModelPackageStatus:
    """Diagnostic status for the complete models package."""

    package_name: str
    ready: bool
    imported_modules: Tuple[str, ...]
    failed_modules: Tuple[str, ...]
    missing_classes: Tuple[str, ...]
    available_classes: Tuple[str, ...]
    records: Tuple[ModelImportRecord, ...]

    def to_dict(self, *, include_tracebacks: bool = False) -> Dict[str, Any]:
        """Serialize package status for debug/status endpoints."""
        return {
            "packageName": self.package_name,
            "ready": self.ready,
            "importedModules": list(self.imported_modules),
            "failedModules": list(self.failed_modules),
            "missingClasses": list(self.missing_classes),
            "availableClasses": list(self.available_classes),
            "records": [
                {
                    "moduleName": record.module_name,
                    "importPath": record.import_path,
                    "imported": record.imported,
                    "error": record.error,
                    "traceback": record.traceback_text if include_tracebacks else None,
                    "exportedSymbols": list(record.exported_symbols),
                }
                for record in self.records
            ],
        }


_MODEL_MODULES: Dict[str, Any] = {}
_MODEL_IMPORT_RECORDS: Dict[str, ModelImportRecord] = {}
_MODEL_CLASSES: Dict[str, Type[Any]] = {}


def _build_import_path(module_name: str) -> str:
    """Build absolute import path for one model module."""
    return f"{PACKAGE_NAME}.{module_name}"


def _safe_import_model_module(module_name: str) -> Optional[Any]:
    """
    Import one model module defensively and cache the result.

    Import failures are recorded so status endpoints can show what went wrong.
    `require_models_ready()` can later turn those recorded failures into a hard
    startup error.
    """
    if module_name in _MODEL_MODULES:
        return _MODEL_MODULES[module_name]

    import_path = _build_import_path(module_name)

    try:
        module = importlib.import_module(import_path)
    except Exception as exc:
        record = ModelImportRecord(
            module_name=module_name,
            import_path=import_path,
            imported=False,
            error=f"{type(exc).__name__}: {exc}",
            traceback_text=traceback.format_exc(),
            exported_symbols=tuple(),
        )
        _MODEL_IMPORT_RECORDS[module_name] = record
        return None

    exported_symbols = tuple(
        sorted(
            symbol
            for symbol in dir(module)
            if not symbol.startswith("_")
        )
    )

    record = ModelImportRecord(
        module_name=module_name,
        import_path=import_path,
        imported=True,
        error=None,
        traceback_text=None,
        exported_symbols=exported_symbols,
    )

    _MODEL_MODULES[module_name] = module
    _MODEL_IMPORT_RECORDS[module_name] = record
    return module


def _load_all_model_modules() -> None:
    """Import all known model modules in stable order."""
    for module_name in MODEL_IMPORT_ORDER:
        _safe_import_model_module(module_name)


def _collect_model_classes() -> Dict[str, Type[Any]]:
    """
    Collect expected model classes from imported modules.

    This function does not raise by default. Missing classes are visible in
    `get_model_package_status()`.
    """
    _load_all_model_modules()

    for class_name, module_name in MODEL_CLASS_TO_MODULE.items():
        if class_name in _MODEL_CLASSES:
            continue

        module = _MODEL_MODULES.get(module_name)
        if module is None:
            continue

        model_class = getattr(module, class_name, None)
        if model_class is not None:
            _MODEL_CLASSES[class_name] = model_class

    return dict(_MODEL_CLASSES)


def reset_model_import_cache() -> None:
    """
    Reset local import/status caches.

    This does not unload Python modules from sys.modules. It only resets this
    package's diagnostic caches.
    """
    _MODEL_MODULES.clear()
    _MODEL_IMPORT_RECORDS.clear()
    _MODEL_CLASSES.clear()
    get_model_package_status.cache_clear()
    get_model_registry.cache_clear()
    get_model_class_map.cache_clear()


@lru_cache(maxsize=1)
def get_model_package_status() -> ModelPackageStatus:
    """
    Return diagnostic status for model registration.

    This is useful for `/projects/_status`, `/chunks/_status` or a later
    `/models/_status` route.
    """
    classes = _collect_model_classes()

    imported_modules: List[str] = []
    failed_modules: List[str] = []
    records: List[ModelImportRecord] = []

    for module_name in MODEL_IMPORT_ORDER:
        record = _MODEL_IMPORT_RECORDS.get(module_name)

        if record is None:
            record = ModelImportRecord(
                module_name=module_name,
                import_path=_build_import_path(module_name),
                imported=False,
                error="module was not imported",
                traceback_text=None,
                exported_symbols=tuple(),
            )

        records.append(record)

        if record.imported:
            imported_modules.append(module_name)
        else:
            failed_modules.append(module_name)

    missing_classes = tuple(
        class_name
        for class_name in EXPECTED_MODEL_CLASSES
        if class_name not in classes
    )

    available_classes = tuple(
        class_name
        for class_name in EXPECTED_MODEL_CLASSES
        if class_name in classes
    )

    ready = not failed_modules and not missing_classes

    return ModelPackageStatus(
        package_name=PACKAGE_NAME,
        ready=ready,
        imported_modules=tuple(imported_modules),
        failed_modules=tuple(failed_modules),
        missing_classes=missing_classes,
        available_classes=available_classes,
        records=tuple(records),
    )


def is_models_package_ready() -> bool:
    """Return True if all expected model modules/classes are available."""
    return get_model_package_status().ready


def require_models_ready() -> None:
    """
    Raise RuntimeError if model imports are incomplete.

    Use this during startup or migration checks when incomplete model
    registration must be treated as a hard error.
    """
    status = get_model_package_status()

    if status.ready:
        return

    details = status.to_dict(include_tracebacks=True)
    raise RuntimeError(
        "vectoplan-chunk models package is not ready. "
        f"Status: {details}"
    )


@lru_cache(maxsize=1)
def get_model_class_map() -> Dict[str, Type[Any]]:
    """Return expected model classes by class name."""
    _collect_model_classes()
    return dict(_MODEL_CLASSES)


@lru_cache(maxsize=1)
def get_model_registry() -> Dict[str, Type[Any]]:
    """
    Return model registry.

    Alias for get_model_class_map(), named for repository/bootstrap usage.
    """
    return get_model_class_map()


def get_model_class(class_name: str) -> Optional[Type[Any]]:
    """Return one model class by name, or None if unavailable."""
    if not class_name:
        return None

    return get_model_class_map().get(str(class_name))


def require_model_class(class_name: str) -> Type[Any]:
    """Return one model class or raise a clear RuntimeError."""
    model_class = get_model_class(class_name)

    if model_class is None:
        status = get_model_package_status()
        raise RuntimeError(
            f"Model class '{class_name}' is not available. "
            f"Missing classes: {list(status.missing_classes)}"
        )

    return model_class


def iter_model_classes() -> Iterable[Type[Any]]:
    """Iterate over available model classes in expected order."""
    class_map = get_model_class_map()

    for class_name in EXPECTED_MODEL_CLASSES:
        model_class = class_map.get(class_name)
        if model_class is not None:
            yield model_class


def get_model_table_names() -> List[str]:
    """Return SQLAlchemy table names for available model classes."""
    table_names: List[str] = []

    for model_class in iter_model_classes():
        table_name = getattr(model_class, "__tablename__", None)
        if table_name:
            table_names.append(str(table_name))

    return table_names


def get_model_debug_summary() -> Dict[str, Any]:
    """Return compact debug summary for status endpoints."""
    status = get_model_package_status()

    return {
        "ready": status.ready,
        "packageName": status.package_name,
        "importedModules": list(status.imported_modules),
        "failedModules": list(status.failed_modules),
        "availableClasses": list(status.available_classes),
        "missingClasses": list(status.missing_classes),
        "tableNames": get_model_table_names(),
    }


def validate_model_instances(instances: Iterable[Any]) -> Dict[str, Dict[str, str]]:
    """
    Run get_validation_errors() on model instances that support it.

    Useful for seed/debug tests.
    """
    result: Dict[str, Dict[str, str]] = {}

    for instance in instances or []:
        if instance is None:
            continue

        name = type(instance).__name__
        key = getattr(instance, "id", None)
        if key is None:
            key = getattr(instance, "project_id", None)
        if key is None:
            key = getattr(instance, "universe_id", None)
        if key is None:
            key = getattr(instance, "world_id", None)
        if key is None:
            key = getattr(instance, "object_instance_id", None)
        if key is None:
            key = getattr(instance, "event_id", None)
        if key is None:
            key = getattr(instance, "command_id", None)
        if key is None:
            key = "unpersisted"

        result_key = f"{name}:{key}"

        validator = getattr(instance, "get_validation_errors", None)
        if callable(validator):
            try:
                errors = validator()
            except Exception as exc:
                errors = {"__validator__": f"{type(exc).__name__}: {exc}"}

            if errors:
                result[result_key] = dict(errors)

    return result


def serialize_model_instance(
    instance: Any,
    *,
    include_internal: bool = False,
    include_content: bool = False,
) -> Dict[str, Any]:
    """
    Serialize one model instance defensively.

    Uses the model's own `to_dict()` if available.
    """
    if instance is None:
        return {}

    serializer = getattr(instance, "to_dict", None)

    if callable(serializer):
        try:
            return serializer(
                include_internal=include_internal,
                include_content=include_content,
            )
        except TypeError:
            try:
                return serializer(include_internal=include_internal)
            except TypeError:
                return serializer()
        except Exception as exc:
            return {
                "type": type(instance).__name__,
                "serializationError": f"{type(exc).__name__}: {exc}",
            }

    return {
        "type": type(instance).__name__,
        "repr": repr(instance),
    }


def serialize_model_instances(
    instances: Iterable[Any],
    *,
    include_internal: bool = False,
    include_content: bool = False,
) -> List[Dict[str, Any]]:
    """Serialize model instances defensively."""
    return [
        serialize_model_instance(
            instance,
            include_internal=include_internal,
            include_content=include_content,
        )
        for instance in instances or []
    ]


# Import all model modules at package import time so SQLAlchemy metadata is
# populated for migrations and db.create_all().
_load_all_model_modules()
_collect_model_classes()


# Public class aliases.
Project = _MODEL_CLASSES.get("Project")
Universe = _MODEL_CLASSES.get("Universe")
WorldInstance = _MODEL_CLASSES.get("WorldInstance")
BlockRegistry = _MODEL_CLASSES.get("BlockRegistry")
BlockType = _MODEL_CLASSES.get("BlockType")
ChunkSnapshot = _MODEL_CLASSES.get("ChunkSnapshot")
WorldCommandLog = _MODEL_CLASSES.get("WorldCommandLog")
ChunkEvent = _MODEL_CLASSES.get("ChunkEvent")
WorldObjectInstance = _MODEL_CLASSES.get("WorldObjectInstance")
WorldObjectChunkRef = _MODEL_CLASSES.get("WorldObjectChunkRef")


__all__ = [
    "PACKAGE_NAME",
    "MODEL_IMPORT_ORDER",
    "EXPECTED_MODEL_CLASSES",
    "MODEL_CLASS_TO_MODULE",
    "ModelImportRecord",
    "ModelPackageStatus",
    "Project",
    "Universe",
    "WorldInstance",
    "BlockRegistry",
    "BlockType",
    "ChunkSnapshot",
    "WorldCommandLog",
    "ChunkEvent",
    "WorldObjectInstance",
    "WorldObjectChunkRef",
    "reset_model_import_cache",
    "get_model_package_status",
    "is_models_package_ready",
    "require_models_ready",
    "get_model_class_map",
    "get_model_registry",
    "get_model_class",
    "require_model_class",
    "iter_model_classes",
    "get_model_table_names",
    "get_model_debug_summary",
    "validate_model_instances",
    "serialize_model_instance",
    "serialize_model_instances",
]