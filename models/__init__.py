# services/vectoplan-chunk/models/__init__.py
"""
Model registration package for vectoplan-chunk.

This package imports and exposes all SQLAlchemy models used by the chunk
service. Importing this package should register every model class with the
Flask-SQLAlchemy metadata so migrations and explicit bootstrap paths can see
the full schema.

Current persistent model groups:

    Project / Universe / WorldInstance
    BlockRegistry / BlockType
    ChunkSnapshot
    WorldCommandLog / ChunkEvent
    WorldObjectInstance / WorldObjectChunkRef

App integration model fields:

    Project.external_app_project_id
    Project.default_universe_id
    Project.default_world_id
    Project.spawn_world_id

    Universe.default_world_id
    Universe.spawn_world_id

    WorldInstance.world_id
    WorldInstance.template_id
    WorldInstance.provider_world_id
    WorldInstance.block_registry_id

Important design rules:
- import order is intentional,
- public/API ids are not internal database ids,
- Project.project_id is the chunk-service public project id,
- Project.external_app_project_id links to vectoplan-app without DB FK,
- Universe.universe_id is unique inside one project,
- WorldInstance.world_id is unique inside one universe,
- ChunkSnapshot uniqueness uses internal world_db_id + chunk_x/y/z,
- ChunkEvent is append-only historical truth,
- ChunkSnapshot is current load-truth,
- WorldObjectInstance prepares future multi-block object support.

This package does not:
- create tables,
- run migrations,
- seed data,
- create default projects,
- create chunk projects for app projects.
"""

from __future__ import annotations

import importlib
import traceback
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple, Type


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

MODEL_CLASS_TO_TABLE: Dict[str, str] = {
    "Project": "projects",
    "Universe": "universes",
    "WorldInstance": "world_instances",
    "BlockRegistry": "block_registries",
    "BlockType": "block_types",
    "ChunkSnapshot": "chunk_snapshots",
    "WorldCommandLog": "world_command_logs",
    "ChunkEvent": "chunk_events",
    "WorldObjectInstance": "world_object_instances",
    "WorldObjectChunkRef": "world_object_chunk_refs",
}

# These are diagnostic expectations only. Missing columns are reported through
# get_model_debug_summary() and require_expected_model_columns(), but this module
# does not raise during import unless explicitly requested.
EXPECTED_MODEL_COLUMNS: Dict[str, Tuple[str, ...]] = {
    "Project": (
        "id",
        "project_id",
        "slug",
        "name",
        "status",
        "schema_version",
        "revision",
        "default_universe_id",
        "default_world_id",
        "spawn_world_id",
        "external_app_project_id",
        "source_service",
        "metadata_json",
        "created_at",
        "updated_at",
        "deleted_at",
    ),
    "Universe": (
        "id",
        "project_db_id",
        "universe_id",
        "slug",
        "name",
        "status",
        "schema_version",
        "revision",
        "universe_role",
        "universe_scope",
        "default_world_id",
        "spawn_world_id",
        "metadata_json",
        "created_at",
        "updated_at",
        "deleted_at",
    ),
    "WorldInstance": (
        "id",
        "project_db_id",
        "universe_db_id",
        "world_id",
        "slug",
        "name",
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
        "metadata_json",
        "created_at",
        "updated_at",
        "deleted_at",
    ),
    "BlockRegistry": (
        "id",
    ),
    "BlockType": (
        "id",
    ),
    "ChunkSnapshot": (
        "id",
    ),
    "WorldCommandLog": (
        "id",
    ),
    "ChunkEvent": (
        "id",
    ),
    "WorldObjectInstance": (
        "id",
    ),
    "WorldObjectChunkRef": (
        "id",
    ),
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
class ModelClassRecord:
    """Diagnostic record for one expected model class."""

    class_name: str
    module_name: str
    table_name: Optional[str]
    available: bool
    columns: Tuple[str, ...] = field(default_factory=tuple)
    missing_expected_columns: Tuple[str, ...] = field(default_factory=tuple)
    relationships: Tuple[str, ...] = field(default_factory=tuple)

    def to_dict(self) -> Dict[str, Any]:
        """Serialize model class record."""
        return {
            "className": self.class_name,
            "moduleName": self.module_name,
            "tableName": self.table_name,
            "available": self.available,
            "columns": list(self.columns),
            "missingExpectedColumns": list(self.missing_expected_columns),
            "relationships": list(self.relationships),
        }


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
    class_records: Tuple[ModelClassRecord, ...] = field(default_factory=tuple)
    missing_expected_columns: Mapping[str, Tuple[str, ...]] = field(default_factory=dict)

    def to_dict(self, *, include_tracebacks: bool = False) -> Dict[str, Any]:
        """Serialize package status for debug/status endpoints."""
        return {
            "packageName": self.package_name,
            "ready": self.ready,
            "importedModules": list(self.imported_modules),
            "failedModules": list(self.failed_modules),
            "missingClasses": list(self.missing_classes),
            "availableClasses": list(self.available_classes),
            "missingExpectedColumns": {
                class_name: list(columns)
                for class_name, columns in self.missing_expected_columns.items()
            },
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
            "classRecords": [
                record.to_dict()
                for record in self.class_records
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
    require_models_ready() can later turn those recorded failures into a hard
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
    get_model_package_status().
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


def _model_columns(model_class: Type[Any] | None) -> Tuple[str, ...]:
    """Return SQLAlchemy column names for a model class."""
    if model_class is None:
        return tuple()

    try:
        table = getattr(model_class, "__table__", None)
        columns = getattr(table, "columns", None)
        if columns is None:
            return tuple()
        return tuple(str(column.name) for column in columns)
    except Exception:
        return tuple()


def _model_relationships(model_class: Type[Any] | None) -> Tuple[str, ...]:
    """Return SQLAlchemy relationship names for a model class."""
    if model_class is None:
        return tuple()

    try:
        mapper = getattr(model_class, "__mapper__", None)
        relationships = getattr(mapper, "relationships", None)
        if relationships is None:
            return tuple()
        return tuple(str(relationship.key) for relationship in relationships)
    except Exception:
        return tuple()


def _model_table_name(model_class: Type[Any] | None) -> Optional[str]:
    """Return SQLAlchemy table name for a model class."""
    if model_class is None:
        return None

    try:
        table_name = getattr(model_class, "__tablename__", None)
        if table_name:
            return str(table_name)

        table = getattr(model_class, "__table__", None)
        table_name = getattr(table, "name", None)
        if table_name:
            return str(table_name)
    except Exception:
        return None

    return None


def _build_model_class_record(class_name: str) -> ModelClassRecord:
    """Build diagnostic record for one expected model class."""
    module_name = MODEL_CLASS_TO_MODULE.get(class_name, "<unknown>")
    model_class = _MODEL_CLASSES.get(class_name)

    columns = _model_columns(model_class)
    relationships = _model_relationships(model_class)
    table_name = _model_table_name(model_class)

    expected_columns = EXPECTED_MODEL_COLUMNS.get(class_name, tuple())
    column_set = set(columns)

    missing_expected_columns = tuple(
        column
        for column in expected_columns
        if column not in column_set
    )

    return ModelClassRecord(
        class_name=class_name,
        module_name=module_name,
        table_name=table_name,
        available=model_class is not None,
        columns=columns,
        missing_expected_columns=missing_expected_columns,
        relationships=relationships,
    )


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
    get_model_table_map.cache_clear()
    get_model_column_map.cache_clear()
    get_model_relationship_map.cache_clear()


@lru_cache(maxsize=1)
def get_model_package_status() -> ModelPackageStatus:
    """
    Return diagnostic status for model registration.

    This is useful for /projects/_status, /chunks/_status or a later
    /models/_status route.
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

    class_records = tuple(
        _build_model_class_record(class_name)
        for class_name in EXPECTED_MODEL_CLASSES
    )

    missing_expected_columns = {
        record.class_name: record.missing_expected_columns
        for record in class_records
        if record.missing_expected_columns
    }

    # `ready` means import/class registration ready. Missing expected columns
    # are reported separately so rolling deployments can inspect status without
    # making package import fail.
    ready = not failed_modules and not missing_classes

    return ModelPackageStatus(
        package_name=PACKAGE_NAME,
        ready=ready,
        imported_modules=tuple(imported_modules),
        failed_modules=tuple(failed_modules),
        missing_classes=missing_classes,
        available_classes=available_classes,
        records=tuple(records),
        class_records=class_records,
        missing_expected_columns=missing_expected_columns,
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


def require_expected_model_columns(
    *,
    class_names: Sequence[str] | None = None,
) -> None:
    """
    Raise RuntimeError if expected diagnostic columns are missing.

    This is intentionally opt-in because older local databases may need an
    explicit migration/bootstrap run before all columns exist.
    """
    status = get_model_package_status()

    selected = set(class_names or EXPECTED_MODEL_CLASSES)
    missing = {
        class_name: columns
        for class_name, columns in status.missing_expected_columns.items()
        if class_name in selected and columns
    }

    if not missing:
        return

    raise RuntimeError(
        "vectoplan-chunk model classes are missing expected columns. "
        f"Missing: {missing}"
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


@lru_cache(maxsize=1)
def get_model_table_map() -> Dict[str, str]:
    """Return model class name -> table name for available model classes."""
    result: Dict[str, str] = {}

    for class_name, model_class in get_model_class_map().items():
        table_name = _model_table_name(model_class)
        if table_name:
            result[class_name] = table_name

    return result


@lru_cache(maxsize=1)
def get_model_column_map() -> Dict[str, Tuple[str, ...]]:
    """Return model class name -> column names for available model classes."""
    result: Dict[str, Tuple[str, ...]] = {}

    for class_name, model_class in get_model_class_map().items():
        result[class_name] = _model_columns(model_class)

    return result


@lru_cache(maxsize=1)
def get_model_relationship_map() -> Dict[str, Tuple[str, ...]]:
    """Return model class name -> relationship names."""
    result: Dict[str, Tuple[str, ...]] = {}

    for class_name, model_class in get_model_class_map().items():
        result[class_name] = _model_relationships(model_class)

    return result


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
        table_name = _model_table_name(model_class)
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
        "tableMap": get_model_table_map(),
        "columnMap": {
            class_name: list(columns)
            for class_name, columns in get_model_column_map().items()
        },
        "relationshipMap": {
            class_name: list(relationships)
            for class_name, relationships in get_model_relationship_map().items()
        },
        "missingExpectedColumns": {
            class_name: list(columns)
            for class_name, columns in status.missing_expected_columns.items()
        },
        "appIntegrationReady": is_app_integration_model_shape_ready(),
        "coreWorldShapeReady": is_core_world_model_shape_ready(),
    }


def is_model_column_available(class_name: str, column_name: str) -> bool:
    """Return whether one model column is available."""
    if not class_name or not column_name:
        return False

    columns = get_model_column_map().get(str(class_name), tuple())
    return str(column_name) in set(columns)


def is_app_integration_model_shape_ready() -> bool:
    """
    Return whether model classes expose the columns needed by app provisioning.
    """
    required = {
        "Project": (
            "project_id",
            "external_app_project_id",
            "default_universe_id",
            "default_world_id",
            "spawn_world_id",
            "metadata_json",
        ),
        "Universe": (
            "project_db_id",
            "universe_id",
            "default_world_id",
            "spawn_world_id",
            "metadata_json",
        ),
        "WorldInstance": (
            "project_db_id",
            "universe_db_id",
            "world_id",
            "template_id",
            "provider_world_id",
            "block_registry_id",
            "metadata_json",
        ),
    }

    column_map = get_model_column_map()

    for class_name, required_columns in required.items():
        available = set(column_map.get(class_name, tuple()))
        for column in required_columns:
            if column not in available:
                return False

    return True


def is_core_world_model_shape_ready() -> bool:
    """
    Return whether model classes expose the core chunk-world columns.
    """
    required = {
        "Project": (
            "id",
            "project_id",
            "status",
        ),
        "Universe": (
            "id",
            "project_db_id",
            "universe_id",
            "status",
        ),
        "WorldInstance": (
            "id",
            "project_db_id",
            "universe_db_id",
            "world_id",
            "chunk_size",
            "cell_size",
            "surface_y",
            "min_y",
            "max_y",
            "block_registry_id",
            "block_registry_version",
        ),
        "ChunkSnapshot": (
            "id",
        ),
        "WorldCommandLog": (
            "id",
        ),
        "ChunkEvent": (
            "id",
        ),
    }

    column_map = get_model_column_map()

    for class_name, required_columns in required.items():
        available = set(column_map.get(class_name, tuple()))
        for column in required_columns:
            if column not in available:
                return False

    return True


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
            key = getattr(instance, "external_app_project_id", None)
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

    Uses the model's own to_dict() if available.
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


def build_model_identity(instance: Any) -> Dict[str, Any]:
    """Build compact identity information for one model instance."""
    if instance is None:
        return {}

    return {
        "type": type(instance).__name__,
        "id": getattr(instance, "id", None),
        "projectId": getattr(instance, "project_id", None),
        "externalAppProjectId": getattr(instance, "external_app_project_id", None),
        "universeId": getattr(instance, "universe_id", None),
        "worldId": getattr(instance, "world_id", None),
        "chunkSnapshotId": getattr(instance, "snapshot_id", None),
        "commandId": getattr(instance, "command_id", None),
        "eventId": getattr(instance, "event_id", None),
        "objectInstanceId": getattr(instance, "object_instance_id", None),
    }


def build_model_schema_report() -> Dict[str, Any]:
    """Build a schema-oriented report for diagnostics and bootstrap output."""
    status = get_model_package_status()

    return {
        "package": status.to_dict(include_tracebacks=False),
        "ready": status.ready,
        "appIntegrationReady": is_app_integration_model_shape_ready(),
        "coreWorldShapeReady": is_core_world_model_shape_ready(),
        "tableNames": get_model_table_names(),
        "tableMap": get_model_table_map(),
        "columnMap": {
            class_name: list(columns)
            for class_name, columns in get_model_column_map().items()
        },
        "relationshipMap": {
            class_name: list(relationships)
            for class_name, relationships in get_model_relationship_map().items()
        },
    }


# Import all model modules at package import time so SQLAlchemy metadata is
# populated for migrations and explicit db.create_all() bootstrap.
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
    "MODEL_CLASS_TO_TABLE",
    "EXPECTED_MODEL_COLUMNS",
    "ModelImportRecord",
    "ModelClassRecord",
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
    "require_expected_model_columns",
    "get_model_class_map",
    "get_model_registry",
    "get_model_table_map",
    "get_model_column_map",
    "get_model_relationship_map",
    "get_model_class",
    "require_model_class",
    "iter_model_classes",
    "get_model_table_names",
    "get_model_debug_summary",
    "is_model_column_available",
    "is_app_integration_model_shape_ready",
    "is_core_world_model_shape_ready",
    "validate_model_instances",
    "serialize_model_instance",
    "serialize_model_instances",
    "build_model_identity",
    "build_model_schema_report",
]