# services/vectoplan-chunk/models/__init__.py
"""
Model registration package for ``vectoplan-chunk``.

Importing this package registers every persistent SQLAlchemy model with the
shared Flask-SQLAlchemy metadata. The module itself remains side-effect free
with regard to database contents: it does not create tables, run migrations,
seed data, provision projects or reconcile access.

Persistent model groups:

    Project / Universe / WorldInstance
    ProjectAccessAssignment
    ProjectRole / ProjectGroup / ProjectGroupMember / ProjectRoleAssignment
    BlockRegistry / BlockType
    ChunkSnapshot
    WorldCommandLog / ChunkEvent
    WorldObjectInstance / WorldObjectChunkRef

Service-boundary rules:

- ``vectoplan-app`` owns App projects and project membership.
- ``vectoplan-chunk`` owns Chunk projects, universes, worlds and the synchronized
  access projection used for enforcement.
- ``Project.external_app_project_id`` is an opaque App-project public id, never a
  foreign key into the App database.
- Direct access assignments contain only canonical ``auth_user_id`` values.
- Local AppUser ids, account ids and e-mail addresses are not user identities.
- Public/API ids are distinct from local database primary keys.
- Viewer and public-viewer access remains read-only in the service layer.
- Import order is intentional because migrations and bootstrap code inspect the
  complete metadata graph.

The package exposes diagnostics for model imports, table/column shape, App
provisioning readiness, the legacy role/group schema and the canonical access
projection schema. Missing columns are reported without mutating the database.
"""


from __future__ import annotations

import hashlib
import importlib
import traceback
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple, Type


PACKAGE_NAME = __name__

MODEL_IMPORT_ORDER: Tuple[str, ...] = (
    "project",
    "project_access_assignment",
    "project_access",
    "universe",
    "world",
    "block",
    "chunk",
    "event",
    "object",
)

EXPECTED_MODEL_CLASSES: Tuple[str, ...] = (
    "Project",
    "ProjectAccessAssignment",
    "ProjectRole",
    "ProjectGroup",
    "ProjectGroupMember",
    "ProjectRoleAssignment",
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
    "ProjectAccessAssignment": "project_access_assignment",
    "ProjectRole": "project_access",
    "ProjectGroup": "project_access",
    "ProjectGroupMember": "project_access",
    "ProjectRoleAssignment": "project_access",
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
    "ProjectAccessAssignment": "project_access_assignments",
    "ProjectRole": "project_roles",
    "ProjectGroup": "project_groups",
    "ProjectGroupMember": "project_group_members",
    "ProjectRoleAssignment": "project_role_assignments",
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
        "owner_auth_user_id",
        "owner_type",
        "owner_id",
        "created_by_auth_user_id",
        "updated_by_auth_user_id",
        "created_by_user_id",
        "updated_by_user_id",
        "world_template_requested",
        "world_template_effective",
        "world_fallback_used",
        "world_fallback_code",
        "earth_reference_fingerprint",
        "world_metadata_json",
        "provisioning_status",
        "provisioning_fingerprint",
        "provisioning_request_id",
        "provisioning_correlation_id",
        "provisioning_error_code",
        "provisioning_retryable",
        "provisioning_repair_required",
        "provisioning_attempts",
        "provisioned_at",
        "provisioning_updated_at",
        "access_sync_status",
        "access_projection_version",
        "access_projection_fingerprint",
        "access_sync_request_id",
        "access_sync_correlation_id",
        "access_sync_error_code",
        "access_sync_retryable",
        "access_sync_repair_required",
        "access_sync_attempts",
        "access_synced_at",
        "access_sync_updated_at",
        "metadata_json",
        "created_at",
        "updated_at",
        "archived_at",
        "deleted_at",
    ),
    "ProjectAccessAssignment": (
        "id",
        "assignment_id",
        "chunk_project_id",
        "auth_user_id",
        "group_id",
        "role",
        "assignment_type",
        "active",
        "managed",
        "source_service",
        "projection_version",
        "projection_fingerprint",
        "request_id",
        "correlation_id",
        "metadata_json",
        "schema_version",
        "revision",
        "created_at",
        "updated_at",
        "deactivated_at",
    ),
    "ProjectRole": (
        "id",
        "role_id",
        "project_db_id",
        "role_key",
        "name",
        "description",
        "permissions_json",
        "is_system",
        "status",
        "schema_version",
        "revision",
        "metadata_json",
        "created_by_user_id",
        "updated_by_user_id",
        "created_at",
        "updated_at",
        "deleted_at",
    ),
    "ProjectGroup": (
        "id",
        "group_id",
        "project_db_id",
        "group_key",
        "name",
        "description",
        "is_system",
        "status",
        "schema_version",
        "revision",
        "metadata_json",
        "created_by_user_id",
        "updated_by_user_id",
        "created_at",
        "updated_at",
        "deleted_at",
    ),
    "ProjectGroupMember": (
        "id",
        "membership_id",
        "project_db_id",
        "group_db_id",
        "group_id",
        "user_id",
        "status",
        "added_by_user_id",
        "removed_by_user_id",
        "starts_at",
        "expires_at",
        "removed_at",
        "removal_reason",
        "schema_version",
        "revision",
        "metadata_json",
        "created_by_user_id",
        "updated_by_user_id",
        "created_at",
        "updated_at",
        "deleted_at",
    ),
    "ProjectRoleAssignment": (
        "id",
        "assignment_id",
        "project_db_id",
        "role_db_id",
        "role_id",
        "subject_type",
        "user_id",
        "group_db_id",
        "group_id",
        "subject_key",
        "permission_overrides_json",
        "status",
        "assigned_by_user_id",
        "revoked_by_user_id",
        "starts_at",
        "expires_at",
        "revoked_at",
        "revocation_reason",
        "schema_version",
        "revision",
        "metadata_json",
        "created_by_user_id",
        "updated_by_user_id",
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
    get_project_access_model_contract.cache_clear()
    get_project_access_projection_contract.cache_clear()
    get_legacy_project_access_model_contract.cache_clear()


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
        "projectAccessShapeReady": is_project_access_model_shape_ready(),
        "accessProjectionShapeReady": is_project_access_projection_model_shape_ready(),
        "legacyProjectAccessShapeReady": is_legacy_project_access_model_shape_ready(),
        "projectAccessContract": get_project_access_model_contract(),
        "coreWorldShapeReady": is_core_world_model_shape_ready(),
    }


def is_model_column_available(class_name: str, column_name: str) -> bool:
    """Return whether one model column is available."""
    if not class_name or not column_name:
        return False

    columns = get_model_column_map().get(str(class_name), tuple())
    return str(column_name) in set(columns)


def _model_shape_has_columns(
    class_name: str,
    required_columns: Sequence[str],
) -> bool:
    """Return whether one registered model exposes all requested DB columns."""
    if not class_name or not required_columns:
        return False

    available_columns = set(get_model_column_map().get(str(class_name), tuple()))
    return all(str(column) in available_columns for column in required_columns)


def is_app_integration_model_shape_ready() -> bool:
    """Return whether models expose the complete App provisioning contract."""
    required = {
        "Project": (
            "project_id",
            "external_app_project_id",
            "source_service",
            "owner_auth_user_id",
            "created_by_auth_user_id",
            "default_universe_id",
            "default_world_id",
            "spawn_world_id",
            "world_template_requested",
            "world_template_effective",
            "world_fallback_used",
            "earth_reference_fingerprint",
            "provisioning_status",
            "provisioning_fingerprint",
            "provisioning_repair_required",
            "access_sync_status",
            "access_projection_fingerprint",
            "access_sync_repair_required",
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

    return all(
        _model_shape_has_columns(class_name, required_columns)
        for class_name, required_columns in required.items()
    )


@lru_cache(maxsize=1)
def get_project_access_projection_contract() -> Dict[str, Any]:
    """Return a DB-free contract for the canonical access projection model."""
    class_name = "ProjectAccessAssignment"
    model_class = get_model_class(class_name)
    expected_columns = EXPECTED_MODEL_COLUMNS.get(class_name, tuple())
    available_columns = _model_columns(model_class)
    missing_columns = tuple(
        column for column in expected_columns if column not in set(available_columns)
    )

    module_record = _MODEL_IMPORT_RECORDS.get("project_access_assignment")
    return {
        "ok": model_class is not None and not missing_columns,
        "moduleName": "project_access_assignment",
        "model": class_name,
        "models": [class_name] if model_class is not None else [],
        "table": _model_table_name(model_class),
        "tables": [
            table_name
            for table_name in (_model_table_name(model_class),)
            if table_name
        ],
        "columns": list(available_columns),
        "expectedColumns": {class_name: list(expected_columns)},
        "missingExpectedColumns": list(missing_columns),
        "importError": (
            module_record.error
            if module_record is not None and not module_record.imported
            else None
        ),
        "canonicalUserIdField": "auth_user_id",
        "projectIdField": "chunk_project_id",
        "roles": ["owner", "admin", "editor", "viewer"],
        "assignmentTypes": ["direct", "group"],
        "viewerReadOnly": True,
        "groupAssignmentsPreserved": True,
    }


@lru_cache(maxsize=1)
def get_legacy_project_access_model_contract() -> Dict[str, Any]:
    """Return the legacy role/group model contract without raising on import."""
    module = _MODEL_MODULES.get("project_access")
    if module is None:
        module = _safe_import_model_module("project_access")

    if module is None:
        record = _MODEL_IMPORT_RECORDS.get("project_access")
        return {
            "ok": False,
            "moduleName": "project_access",
            "error": record.error if record is not None else "module unavailable",
            "models": [],
            "tables": [],
            "expectedColumns": {},
        }

    builder = getattr(module, "get_project_access_model_contract", None)
    if not callable(builder):
        return {
            "ok": False,
            "moduleName": "project_access",
            "error": "get_project_access_model_contract() is missing",
            "models": [],
            "tables": [],
            "expectedColumns": {},
        }

    try:
        result = builder()
    except Exception as exc:
        return {
            "ok": False,
            "moduleName": "project_access",
            "error": f"{type(exc).__name__}: {exc}",
            "models": [],
            "tables": [],
            "expectedColumns": {},
        }

    if not isinstance(result, Mapping):
        return {
            "ok": False,
            "moduleName": "project_access",
            "error": "project access contract is not a mapping",
            "models": [],
            "tables": [],
            "expectedColumns": {},
        }

    normalized = dict(result)
    normalized.setdefault("ok", True)
    normalized.setdefault("moduleName", "project_access")
    return normalized


@lru_cache(maxsize=1)
def get_project_access_model_contract() -> Dict[str, Any]:
    """Return the combined canonical and legacy project-access model contract."""
    projection = get_project_access_projection_contract()
    legacy = get_legacy_project_access_model_contract()

    models = []
    tables = []
    for contract in (projection, legacy):
        for model_name in contract.get("models", []) or []:
            if model_name not in models:
                models.append(model_name)
        for table_name in contract.get("tables", []) or []:
            if table_name not in tables:
                tables.append(table_name)

    expected_columns: Dict[str, Any] = {}
    for contract in (projection, legacy):
        candidate = contract.get("expectedColumns", {})
        if isinstance(candidate, Mapping):
            expected_columns.update(dict(candidate))

    return {
        "ok": bool(projection.get("ok")) and bool(legacy.get("ok")),
        "moduleName": "project_access",
        "canonicalProjection": projection,
        "legacyRoleGroups": legacy,
        "models": models,
        "tables": tables,
        "expectedColumns": expected_columns,
        "canonicalUserIdField": "auth_user_id",
        "sourceOfTruth": "vectoplan-app",
        "viewerReadOnly": True,
    }


def is_project_access_projection_model_shape_ready() -> bool:
    """Return whether the canonical synchronized assignment model is ready."""
    required = EXPECTED_MODEL_COLUMNS.get("ProjectAccessAssignment", tuple())
    project_required = (
        "project_id",
        "owner_auth_user_id",
        "access_sync_status",
        "access_projection_version",
        "access_projection_fingerprint",
        "access_sync_repair_required",
        "access_sync_updated_at",
    )
    return (
        bool(required)
        and _model_shape_has_columns("ProjectAccessAssignment", required)
        and _model_shape_has_columns("Project", project_required)
    )


def is_legacy_project_access_model_shape_ready() -> bool:
    """Return whether the pre-existing role/group schema is registered."""
    required_class_names = (
        "ProjectRole",
        "ProjectGroup",
        "ProjectGroupMember",
        "ProjectRoleAssignment",
    )

    for class_name in required_class_names:
        required_columns = EXPECTED_MODEL_COLUMNS.get(class_name, tuple())
        if not required_columns:
            return False
        if not _model_shape_has_columns(class_name, required_columns):
            return False

    return True


def is_project_access_model_shape_ready() -> bool:
    """Return whether canonical projection and legacy group models are both ready."""
    return (
        is_project_access_projection_model_shape_ready()
        and is_legacy_project_access_model_shape_ready()
    )


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
            key = getattr(instance, "role_id", None)
        if key is None:
            key = getattr(instance, "group_id", None)
        if key is None:
            key = getattr(instance, "membership_id", None)
        if key is None:
            key = getattr(instance, "assignment_id", None)
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
    """Serialize one model instance without leaking private identities by default."""
    if instance is None:
        return {}

    if not include_internal:
        public_serializer = getattr(instance, "to_public_dict", None)
        if callable(public_serializer):
            try:
                result = public_serializer()
                if isinstance(result, Mapping):
                    return dict(result)
            except Exception:
                pass

    serializer = getattr(instance, "to_dict", None)
    if callable(serializer):
        attempts = (
            {
                "include_internal": include_internal,
                "include_content": include_content,
            },
            {
                "include_internal": include_internal,
                "include_private": include_internal,
                "include_metadata": include_content,
            },
            {
                "include_internal": include_internal,
                "include_metadata": include_content,
            },
            {"include_internal": include_internal},
            {},
        )

        last_error: Exception | None = None
        for kwargs in attempts:
            try:
                result = serializer(**kwargs)
                if isinstance(result, Mapping):
                    return dict(result)
                return {
                    "type": type(instance).__name__,
                    "serializationError": "model serializer did not return a mapping",
                }
            except TypeError as exc:
                last_error = exc
                continue
            except Exception as exc:
                return {
                    "type": type(instance).__name__,
                    "serializationError": f"{type(exc).__name__}: {exc}",
                }

        return {
            "type": type(instance).__name__,
            "serializationError": (
                f"{type(last_error).__name__}: {last_error}"
                if last_error is not None
                else "no compatible serializer signature"
            ),
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


def _fingerprint_model_identifier(value: Any) -> Optional[str]:
    """Return a short non-reversible fingerprint for diagnostic identities."""
    if value is None:
        return None
    try:
        text = str(value).strip()
    except Exception:
        return None
    if not text:
        return None
    try:
        return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
    except Exception:
        return None


def build_model_identity(
    instance: Any,
    *,
    include_internal: bool = False,
    include_private_identifiers: bool = False,
) -> Dict[str, Any]:
    """Build compact diagnostic identity without exposing users by default."""
    if instance is None:
        return {}

    result: Dict[str, Any] = {
        "type": type(instance).__name__,
        "projectId": getattr(
            instance,
            "project_id",
            getattr(instance, "chunk_project_id", None),
        ),
        "externalAppProjectId": getattr(instance, "external_app_project_id", None),
        "universeId": getattr(instance, "universe_id", None),
        "worldId": getattr(instance, "world_id", None),
        "roleId": getattr(instance, "role_id", None),
        "membershipId": getattr(instance, "membership_id", None),
        "assignmentId": getattr(instance, "assignment_id", None),
        "subjectType": getattr(
            instance,
            "assignment_type",
            getattr(instance, "subject_type", None),
        ),
        "chunkSnapshotId": getattr(instance, "snapshot_id", None),
        "commandId": getattr(instance, "command_id", None),
        "eventId": getattr(instance, "event_id", None),
        "objectInstanceId": getattr(instance, "object_instance_id", None),
    }

    auth_user_id = getattr(
        instance,
        "auth_user_id",
        getattr(instance, "user_id", None),
    )
    group_id = getattr(instance, "group_id", None)
    owner_auth_user_id = getattr(instance, "owner_auth_user_id", None)

    if include_private_identifiers:
        result["authUserId"] = auth_user_id
        result["groupId"] = group_id
        result["ownerAuthUserId"] = owner_auth_user_id
    else:
        result["authUserFingerprint"] = _fingerprint_model_identifier(auth_user_id)
        result["groupFingerprint"] = _fingerprint_model_identifier(group_id)
        result["ownerFingerprint"] = _fingerprint_model_identifier(owner_auth_user_id)

    if include_internal:
        result["id"] = getattr(instance, "id", None)

    return {
        key: value
        for key, value in result.items()
        if value is not None
    }


def build_model_schema_report() -> Dict[str, Any]:
    """Build a schema-oriented report for diagnostics and bootstrap output."""
    status = get_model_package_status()

    return {
        "package": status.to_dict(include_tracebacks=False),
        "ready": status.ready,
        "appIntegrationReady": is_app_integration_model_shape_ready(),
        "projectAccessShapeReady": is_project_access_model_shape_ready(),
        "accessProjectionShapeReady": is_project_access_projection_model_shape_ready(),
        "legacyProjectAccessShapeReady": is_legacy_project_access_model_shape_ready(),
        "projectAccessContract": get_project_access_model_contract(),
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
ProjectAccessAssignment = _MODEL_CLASSES.get("ProjectAccessAssignment")
ProjectRole = _MODEL_CLASSES.get("ProjectRole")
ProjectGroup = _MODEL_CLASSES.get("ProjectGroup")
ProjectGroupMember = _MODEL_CLASSES.get("ProjectGroupMember")
ProjectRoleAssignment = _MODEL_CLASSES.get("ProjectRoleAssignment")
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
    "ProjectAccessAssignment",
    "ProjectRole",
    "ProjectGroup",
    "ProjectGroupMember",
    "ProjectRoleAssignment",
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
    "get_project_access_projection_contract",
    "get_legacy_project_access_model_contract",
    "get_project_access_model_contract",
    "is_project_access_projection_model_shape_ready",
    "is_legacy_project_access_model_shape_ready",
    "is_project_access_model_shape_ready",
    "is_core_world_model_shape_ready",
    "validate_model_instances",
    "serialize_model_instance",
    "serialize_model_instances",
    "build_model_identity",
    "build_model_schema_report",
]