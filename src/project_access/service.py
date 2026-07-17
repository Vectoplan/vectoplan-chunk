# services/vectoplan-chunk/src/project_access/service.py
"""
Transaktionsneutrale Orchestrierung für vorbereitete Projektzugriffe.

Dieses Modul verbindet die persistenten Modelle aus
``models/project_access.py`` zu idempotenten, projektgescopten Operationen.
Es speichert Rollen, Gruppen, Mitgliedschaften und Rollenzuweisungen, führt
aber ausdrücklich noch keine Authentifizierung oder Autorisierung aus.

Harte Architekturregeln
-----------------------
* Jede Operation ist über ``Project.id`` projektgescopt.
* Externe User-IDs bleiben Strings ohne Foreign Key zu anderen Services.
* Rollen- und Gruppenreferenzen müssen zum selben Projekt gehören.
* Das Modul führt niemals ``commit()`` aus.
* Das Modul führt niemals einen globalen ``rollback()`` aus.
* Explizites ``flush()`` ist erlaubt, damit interne IDs für abhängige Zeilen
  verfügbar werden und Constraintfehler früh sichtbar sind.
* Bei einem Flushfehler muss die aufrufende Transaktionsgrenze rollbacken.
* ORM-Objekte, Queryresultate und Berechtigungszustände werden nie
  prozesslokal gecacht.
* Caches sind ausschließlich für reine Normalisierung und unveränderliche
  Standardrollenvorlagen vorgesehen.
* Effektive Rechte, Vererbung und Route-Schutz gehören in eine spätere
  Autorisierungsschicht.

Typischer Aufruf innerhalb einer bestehenden Route oder Provisionierung::

    result = ensure_project_access_initialized(
        project=project,
        owner_user_id="1",
        actor_user_id="1",
        session=db.session,
        flush=True,
    )
    db.session.commit()

Die aufrufende Schicht besitzt weiterhin die einzige Commit-/Rollback-Grenze.
"""

from __future__ import annotations

import copy
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from functools import lru_cache
from typing import Any, Final, Iterable, Mapping, MutableMapping, Optional, Sequence

from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from extensions import db
from models.project import Project
from models.project_access import (
    ASSIGNMENT_STATUS_ACTIVE,
    ASSIGNMENT_STATUS_DELETED,
    ASSIGNMENT_STATUS_INACTIVE,
    ASSIGNMENT_STATUS_REVOKED,
    DEFAULT_PROJECT_ROLE_KEYS,
    DEFAULT_ROLE_ADMIN,
    DEFAULT_ROLE_EDITOR,
    DEFAULT_ROLE_OWNER,
    DEFAULT_ROLE_VIEWER,
    GROUP_STATUS_ACTIVE,
    GROUP_STATUS_DELETED,
    MEMBERSHIP_STATUS_ACTIVE,
    MEMBERSHIP_STATUS_DELETED,
    MEMBERSHIP_STATUS_INACTIVE,
    MEMBERSHIP_STATUS_REMOVED,
    ROLE_STATUS_ACTIVE,
    ROLE_STATUS_DELETED,
    SUBJECT_TYPE_GROUP,
    SUBJECT_TYPE_USER,
    ProjectGroup,
    ProjectGroupMember,
    ProjectRole,
    ProjectRoleAssignment,
    get_default_project_role_definitions,
)


LOGGER = logging.getLogger(__name__)

PROJECT_ACCESS_SERVICE_VERSION: Final[str] = "1.0.0"
PROJECT_ACCESS_SERVICE_SCHEMA_VERSION: Final[int] = 1

MAX_EXTERNAL_USER_ID_LENGTH: Final[int] = 191
MAX_PUBLIC_ID_LENGTH: Final[int] = 96
MAX_KEY_LENGTH: Final[int] = 80
MAX_REASON_LENGTH: Final[int] = 1000

_KEY_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[a-z0-9][a-z0-9._:-]*$")
_CONTROL_CHARACTER_PATTERN: Final[re.Pattern[str]] = re.compile(r"[\x00-\x1f\x7f]")


# ---------------------------------------------------------------------------
# Fehlerverträge
# ---------------------------------------------------------------------------


class ProjectAccessServiceError(RuntimeError):
    """Basisklasse für kontrollierte Fehler der Access-Service-Schicht."""

    default_code: Final[str] = "project_access_service_error"

    def __init__(
        self,
        message: str,
        *,
        code: Optional[str] = None,
        details: Optional[Mapping[str, Any]] = None,
        cause: Optional[BaseException] = None,
    ) -> None:
        super().__init__(message)
        self.code = str(code or self.default_code)
        self.details = dict(details or {})
        self.cause = cause

    def to_dict(self, *, include_cause: bool = False) -> dict[str, Any]:
        result: dict[str, Any] = {
            "ok": False,
            "error": self.code,
            "message": str(self),
            "details": _make_json_safe(self.details),
        }
        if include_cause and self.cause is not None:
            result["cause"] = (
                f"{type(self.cause).__name__}: {self.cause}"
            )
        return result


class ProjectAccessValidationError(ProjectAccessServiceError):
    default_code = "project_access_validation_failed"


class ProjectAccessNotFoundError(ProjectAccessServiceError):
    default_code = "project_access_not_found"


class ProjectAccessConflictError(ProjectAccessServiceError):
    default_code = "project_access_conflict"


class ProjectAccessCrossProjectError(ProjectAccessServiceError):
    default_code = "project_access_cross_project_reference"


class ProjectAccessInvariantError(ProjectAccessServiceError):
    default_code = "project_access_invariant_failed"


class ProjectAccessPersistenceError(ProjectAccessServiceError):
    default_code = "project_access_persistence_failed"


# ---------------------------------------------------------------------------
# Ergebnisverträge
# ---------------------------------------------------------------------------


@dataclass
class MutationStats:
    """Kleine, JSON-sichere Statistik für idempotente Mutationen."""

    created: int = 0
    updated: int = 0
    reactivated: int = 0
    reused: int = 0
    revoked: int = 0
    removed: int = 0
    deleted: int = 0
    skipped: int = 0

    @property
    def changed(self) -> bool:
        return any(
            value > 0
            for value in (
                self.created,
                self.updated,
                self.reactivated,
                self.revoked,
                self.removed,
                self.deleted,
            )
        )

    def merge(self, other: "MutationStats") -> "MutationStats":
        if not isinstance(other, MutationStats):
            return self
        self.created += other.created
        self.updated += other.updated
        self.reactivated += other.reactivated
        self.reused += other.reused
        self.revoked += other.revoked
        self.removed += other.removed
        self.deleted += other.deleted
        self.skipped += other.skipped
        return self

    def to_dict(self) -> dict[str, Any]:
        return {
            "created": self.created,
            "updated": self.updated,
            "reactivated": self.reactivated,
            "reused": self.reused,
            "revoked": self.revoked,
            "removed": self.removed,
            "deleted": self.deleted,
            "skipped": self.skipped,
            "changed": self.changed,
        }


@dataclass
class EntityMutationResult:
    """Ergebnis einer einzelnen idempotenten Modelmutation."""

    entity: Any
    action: str
    stats: MutationStats = field(default_factory=MutationStats)
    warnings: list[str] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return self.stats.changed

    def to_dict(
        self,
        *,
        include_internal: bool = False,
        include_metadata: bool = True,
    ) -> dict[str, Any]:
        return {
            "action": self.action,
            "changed": self.changed,
            "stats": self.stats.to_dict(),
            "entity": _serialize_entity(
                self.entity,
                include_internal=include_internal,
                include_metadata=include_metadata,
            ),
            "warnings": list(self.warnings),
        }


@dataclass
class ProjectAccessInitializationResult:
    """Ergebnis der Standardrollen- und Owner-Initialisierung."""

    project: Any
    owner_user_id: Optional[str]
    roles_by_key: dict[str, ProjectRole]
    owner_assignment: Optional[ProjectRoleAssignment]
    role_stats: MutationStats = field(default_factory=MutationStats)
    assignment_stats: MutationStats = field(default_factory=MutationStats)
    warnings: list[str] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return self.role_stats.changed or self.assignment_stats.changed

    @property
    def access_initialized(self) -> bool:
        roles_ready = all(
            role_key in self.roles_by_key
            for role_key in DEFAULT_PROJECT_ROLE_KEYS
        )
        owner_ready = (
            self.owner_user_id is None
            or self.owner_assignment is not None
        )
        return roles_ready and owner_ready

    def to_dict(
        self,
        *,
        include_internal: bool = False,
        include_metadata: bool = True,
    ) -> dict[str, Any]:
        project_db_id = getattr(self.project, "id", None)
        project_id = getattr(self.project, "project_id", None)
        return {
            "ok": True,
            "serviceVersion": PROJECT_ACCESS_SERVICE_VERSION,
            "projectDbId": project_db_id if include_internal else None,
            "projectId": project_id,
            "ownerUserId": self.owner_user_id,
            "accessInitialized": self.access_initialized,
            "changed": self.changed,
            "roleStats": self.role_stats.to_dict(),
            "assignmentStats": self.assignment_stats.to_dict(),
            "roles": {
                key: _serialize_entity(
                    role,
                    include_internal=include_internal,
                    include_metadata=include_metadata,
                )
                for key, role in sorted(self.roles_by_key.items())
            },
            "ownerAssignment": _serialize_entity(
                self.owner_assignment,
                include_internal=include_internal,
                include_metadata=include_metadata,
            ) if self.owner_assignment is not None else None,
            "warnings": list(self.warnings),
        }


@dataclass
class ProjectAccessDeleteResult:
    """Ergebnis eines projektweiten Access-Soft-Deletes."""

    project: Any
    stats: MutationStats
    role_count: int = 0
    group_count: int = 0
    membership_count: int = 0
    assignment_count: int = 0
    warnings: list[str] = field(default_factory=list)

    def to_dict(self, *, include_internal: bool = False) -> dict[str, Any]:
        return {
            "ok": True,
            "serviceVersion": PROJECT_ACCESS_SERVICE_VERSION,
            "projectId": getattr(self.project, "project_id", None),
            "projectDbId": getattr(self.project, "id", None)
            if include_internal else None,
            "changed": self.stats.changed,
            "stats": self.stats.to_dict(),
            "counts": {
                "roles": self.role_count,
                "groups": self.group_count,
                "memberships": self.membership_count,
                "assignments": self.assignment_count,
            },
            "warnings": list(self.warnings),
        }


# ---------------------------------------------------------------------------
# Reine Hilfsfunktionen und Caches
# ---------------------------------------------------------------------------


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _make_json_safe(value: Any, *, depth: int = 0) -> Any:
    if depth > 12:
        return repr(value)
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, datetime):
        current = value
        if current.tzinfo is None:
            current = current.replace(tzinfo=timezone.utc)
        return current.astimezone(timezone.utc).isoformat()
    if isinstance(value, Mapping):
        return {
            str(key): _make_json_safe(item, depth=depth + 1)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_make_json_safe(item, depth=depth + 1) for item in value]
    serializer = getattr(value, "to_dict", None)
    if callable(serializer):
        try:
            return _make_json_safe(serializer(), depth=depth + 1)
        except Exception:
            pass
    return repr(value)


@lru_cache(maxsize=512)
def _normalize_key_cached(raw_value: str) -> str:
    value = raw_value.strip().lower().replace(" ", "_")
    value = re.sub(r"_+", "_", value)
    if not value:
        raise ValueError("must not be empty")
    if len(value) > MAX_KEY_LENGTH:
        raise ValueError(f"must not exceed {MAX_KEY_LENGTH} characters")
    if _CONTROL_CHARACTER_PATTERN.search(value):
        raise ValueError("must not contain control characters")
    if not _KEY_PATTERN.fullmatch(value):
        raise ValueError(
            "must start with a-z or 0-9 and contain only "
            "a-z, 0-9, dot, underscore, colon or hyphen"
        )
    return value


def _normalize_key(value: Any, *, field_name: str) -> str:
    if value is None:
        raise ProjectAccessValidationError(
            f"{field_name} is required",
            details={"field": field_name},
        )
    try:
        return _normalize_key_cached(str(value))
    except (TypeError, ValueError) as exc:
        raise ProjectAccessValidationError(
            f"invalid {field_name}: {exc}",
            details={"field": field_name, "value": repr(value)},
            cause=exc,
        ) from exc


@lru_cache(maxsize=1024)
def _normalize_user_id_cached(raw_value: str) -> str:
    value = raw_value.strip()
    if not value:
        raise ValueError("must not be empty")
    if len(value) > MAX_EXTERNAL_USER_ID_LENGTH:
        raise ValueError(
            f"must not exceed {MAX_EXTERNAL_USER_ID_LENGTH} characters"
        )
    if _CONTROL_CHARACTER_PATTERN.search(value):
        raise ValueError("must not contain control characters")
    return value


def _normalize_user_id(
    value: Any,
    *,
    field_name: str,
    required: bool = True,
) -> Optional[str]:
    if value is None:
        if required:
            raise ProjectAccessValidationError(
                f"{field_name} is required",
                details={"field": field_name},
            )
        return None
    try:
        return _normalize_user_id_cached(str(value))
    except (TypeError, ValueError) as exc:
        raise ProjectAccessValidationError(
            f"invalid {field_name}: {exc}",
            details={"field": field_name, "value": repr(value)},
            cause=exc,
        ) from exc


def _normalize_positive_int(value: Any, *, field_name: str) -> int:
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise ProjectAccessValidationError(
            f"{field_name} must be an integer",
            details={"field": field_name, "value": repr(value)},
            cause=exc,
        ) from exc
    if result < 1:
        raise ProjectAccessValidationError(
            f"{field_name} must be greater than zero",
            details={"field": field_name, "value": result},
        )
    return result


def _normalize_optional_public_id(value: Any, *, field_name: str) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if len(text) > MAX_PUBLIC_ID_LENGTH:
        raise ProjectAccessValidationError(
            f"{field_name} must not exceed {MAX_PUBLIC_ID_LENGTH} characters",
            details={"field": field_name},
        )
    if _CONTROL_CHARACTER_PATTERN.search(text):
        raise ProjectAccessValidationError(
            f"{field_name} must not contain control characters",
            details={"field": field_name},
        )
    return text


def _normalize_required_public_id(value: Any, *, field_name: str) -> str:
    normalized = _normalize_optional_public_id(value, field_name=field_name)
    if normalized is None:
        raise ProjectAccessValidationError(
            f"{field_name} is required",
            details={"field": field_name},
        )
    return normalized


def _normalize_reason(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if len(text) > MAX_REASON_LENGTH:
        raise ProjectAccessValidationError(
            f"reason must not exceed {MAX_REASON_LENGTH} characters"
        )
    if _CONTROL_CHARACTER_PATTERN.search(text):
        raise ProjectAccessValidationError(
            "reason must not contain control characters"
        )
    return text


@lru_cache(maxsize=1)
def _default_role_template_cache() -> tuple[tuple[str, dict[str, Any]], ...]:
    try:
        raw_definitions = get_default_project_role_definitions()
    except Exception as exc:
        raise ProjectAccessInvariantError(
            "default project role definitions could not be loaded",
            cause=exc,
        ) from exc

    by_key: list[tuple[str, dict[str, Any]]] = []
    seen: set[str] = set()
    for raw in raw_definitions:
        if not isinstance(raw, Mapping):
            raise ProjectAccessInvariantError(
                "default project role definition must be an object",
                details={"definition": repr(raw)},
            )
        role_key = _normalize_key(raw.get("roleKey"), field_name="roleKey")
        if role_key in seen:
            raise ProjectAccessInvariantError(
                "duplicate default project role key",
                details={"roleKey": role_key},
            )
        seen.add(role_key)
        by_key.append((role_key, copy.deepcopy(dict(raw))))

    expected = set(DEFAULT_PROJECT_ROLE_KEYS)
    available = {key for key, _ in by_key}
    missing = sorted(expected - available)
    if missing:
        raise ProjectAccessInvariantError(
            "default project role definitions are incomplete",
            details={"missingRoleKeys": missing},
        )

    return tuple(by_key)


def get_default_role_templates() -> dict[str, dict[str, Any]]:
    """Liefert eine tiefe Kopie der gecachten Standardrollenvorlagen."""

    return {
        role_key: copy.deepcopy(definition)
        for role_key, definition in _default_role_template_cache()
    }


def clear_project_access_service_caches() -> dict[str, Any]:
    """Leert nur reine Service-Caches, niemals Datenbank- oder ORM-Zustand."""

    before = {
        "key": _normalize_key_cached.cache_info()._asdict(),
        "userId": _normalize_user_id_cached.cache_info()._asdict(),
        "defaultRoleTemplates": _default_role_template_cache.cache_info()._asdict(),
    }
    _normalize_key_cached.cache_clear()
    _normalize_user_id_cached.cache_clear()
    _default_role_template_cache.cache_clear()
    return {
        "cleared": True,
        "before": before,
        "after": {
            "key": _normalize_key_cached.cache_info()._asdict(),
            "userId": _normalize_user_id_cached.cache_info()._asdict(),
            "defaultRoleTemplates": _default_role_template_cache.cache_info()._asdict(),
        },
    }


# ---------------------------------------------------------------------------
# Session-, Query- und Serialisierungshilfen
# ---------------------------------------------------------------------------


def _resolve_session(session: Any = None) -> Any:
    current = session if session is not None else db.session
    required_methods = ("add", "flush", "query")
    missing = [name for name in required_methods if not callable(getattr(current, name, None))]
    if missing:
        raise ProjectAccessInvariantError(
            "invalid SQLAlchemy session",
            details={"missingMethods": missing},
        )
    return current


def _flush_session(session: Any, *, objects: Optional[Sequence[Any]] = None) -> None:
    try:
        if objects:
            session.flush(list(objects))
        else:
            session.flush()
    except IntegrityError as exc:
        raise ProjectAccessConflictError(
            "database constraint rejected project access mutation",
            code="project_access_integrity_conflict",
            details={
                "databaseError": type(getattr(exc, "orig", exc)).__name__,
                "rollbackRequired": True,
            },
            cause=exc,
        ) from exc
    except SQLAlchemyError as exc:
        raise ProjectAccessPersistenceError(
            "project access mutation could not be flushed",
            details={"rollbackRequired": True},
            cause=exc,
        ) from exc
    except Exception as exc:
        raise ProjectAccessPersistenceError(
            "unexpected error while flushing project access mutation",
            details={"rollbackRequired": True},
            cause=exc,
        ) from exc


def _query_all(query: Any) -> list[Any]:
    try:
        return list(query.all())
    except SQLAlchemyError as exc:
        raise ProjectAccessPersistenceError(
            "project access query failed",
            cause=exc,
        ) from exc


def _query_one_or_none(query: Any) -> Any:
    try:
        return query.one_or_none()
    except SQLAlchemyError as exc:
        raise ProjectAccessPersistenceError(
            "project access lookup failed",
            cause=exc,
        ) from exc


def _with_optional_lock(query: Any, *, lock: bool) -> Any:
    if not lock:
        return query
    try:
        return query.with_for_update()
    except Exception:
        return query


def _filter_not_deleted(query: Any, model: Any, *, include_deleted: bool) -> Any:
    if include_deleted:
        return query
    try:
        query = query.filter(model.deleted_at.is_(None))
    except Exception:
        pass
    try:
        query = query.filter(model.status != "deleted")
    except Exception:
        pass
    return query


def _serialize_entity(
    entity: Any,
    *,
    include_internal: bool = False,
    include_metadata: bool = True,
) -> dict[str, Any]:
    if entity is None:
        return {}
    serializer = getattr(entity, "to_dict", None)
    if callable(serializer):
        try:
            return _make_json_safe(
                serializer(
                    include_internal=include_internal,
                    include_metadata=include_metadata,
                )
            )
        except TypeError:
            try:
                return _make_json_safe(
                    serializer(include_internal=include_internal)
                )
            except TypeError:
                try:
                    return _make_json_safe(serializer())
                except Exception as exc:
                    return {
                        "type": type(entity).__name__,
                        "serializationError": f"{type(exc).__name__}: {exc}",
                    }
        except Exception as exc:
            return {
                "type": type(entity).__name__,
                "serializationError": f"{type(exc).__name__}: {exc}",
            }
    return {
        "type": type(entity).__name__,
        "id": getattr(entity, "id", None) if include_internal else None,
        "repr": repr(entity),
    }


def _is_deleted(entity: Any) -> bool:
    try:
        if bool(getattr(entity, "is_deleted")):
            return True
    except Exception:
        pass
    return (
        getattr(entity, "deleted_at", None) is not None
        or getattr(entity, "status", None) == "deleted"
    )


def _require_project_db_id(project: Any) -> int:
    if project is None:
        raise ProjectAccessValidationError("project is required")
    return _normalize_positive_int(
        getattr(project, "id", None),
        field_name="project.id",
    )


def _require_same_project(
    *,
    project_db_id: int,
    entity: Any,
    entity_name: str,
) -> None:
    entity_project_db_id = _normalize_positive_int(
        getattr(entity, "project_db_id", None),
        field_name=f"{entity_name}.project_db_id",
    )
    if entity_project_db_id != project_db_id:
        raise ProjectAccessCrossProjectError(
            f"{entity_name} belongs to another project",
            details={
                "expectedProjectDbId": project_db_id,
                "actualProjectDbId": entity_project_db_id,
                "entityType": type(entity).__name__,
            },
        )


def _mark_changed_once(entity: Any, *, actor_user_id: Optional[str]) -> None:
    toucher = getattr(entity, "touch", None)
    if callable(toucher):
        toucher(updated_by_user_id=actor_user_id)


# ---------------------------------------------------------------------------
# Projektauflösung
# ---------------------------------------------------------------------------


def resolve_project(
    *,
    session: Any = None,
    project: Any = None,
    project_db_id: Any = None,
    project_id: Any = None,
    external_app_project_id: Any = None,
    include_deleted: bool = False,
    lock: bool = False,
) -> Any:
    """Löst genau ein Chunk-Projekt auf und prüft den Löschzustand."""

    current_session = _resolve_session(session)

    if project is not None:
        _require_project_db_id(project)
        if not include_deleted and _is_deleted(project):
            raise ProjectAccessNotFoundError(
                "project is deleted",
                code="project_deleted",
                details={"projectId": getattr(project, "project_id", None)},
            )
        return project

    selectors = [
        project_db_id is not None,
        project_id is not None and str(project_id).strip() != "",
        external_app_project_id is not None
        and str(external_app_project_id).strip() != "",
    ]
    if sum(bool(item) for item in selectors) != 1:
        raise ProjectAccessValidationError(
            "exactly one project selector is required",
            details={
                "acceptedSelectors": [
                    "project",
                    "project_db_id",
                    "project_id",
                    "external_app_project_id",
                ]
            },
        )

    query = current_session.query(Project)
    if project_db_id is not None:
        query = query.filter(
            Project.id == _normalize_positive_int(
                project_db_id,
                field_name="project_db_id",
            )
        )
    elif project_id is not None:
        normalized_project_id = _normalize_required_public_id(
            project_id,
            field_name="project_id",
        )
        query = query.filter(Project.project_id == normalized_project_id)
    else:
        normalized_external_id = _normalize_required_public_id(
            external_app_project_id,
            field_name="external_app_project_id",
        )
        query = query.filter(
            Project.external_app_project_id == normalized_external_id
        )

    query = _with_optional_lock(query, lock=lock)
    resolved = _query_one_or_none(query)
    if resolved is None:
        raise ProjectAccessNotFoundError(
            "project was not found",
            code="project_not_found",
        )
    if not include_deleted and _is_deleted(resolved):
        raise ProjectAccessNotFoundError(
            "project is deleted",
            code="project_deleted",
            details={"projectId": getattr(resolved, "project_id", None)},
        )
    return resolved


# ---------------------------------------------------------------------------
# Rollen-Lookups und Standardrollen
# ---------------------------------------------------------------------------


def list_project_roles(
    *,
    project: Any = None,
    project_db_id: Any = None,
    session: Any = None,
    include_deleted: bool = False,
    lock: bool = False,
) -> list[ProjectRole]:
    current_session = _resolve_session(session)
    resolved_project_db_id = (
        _require_project_db_id(project)
        if project is not None
        else _normalize_positive_int(project_db_id, field_name="project_db_id")
    )
    query = current_session.query(ProjectRole).filter(
        ProjectRole.project_db_id == resolved_project_db_id
    )
    query = _filter_not_deleted(
        query,
        ProjectRole,
        include_deleted=include_deleted,
    )
    query = _with_optional_lock(query, lock=lock)
    try:
        query = query.order_by(ProjectRole.role_key.asc(), ProjectRole.id.asc())
    except Exception:
        pass
    return _query_all(query)


def find_project_role(
    *,
    project: Any = None,
    project_db_id: Any = None,
    role_key: Any = None,
    role_id: Any = None,
    session: Any = None,
    include_deleted: bool = False,
    lock: bool = False,
    required: bool = False,
) -> Optional[ProjectRole]:
    current_session = _resolve_session(session)
    resolved_project_db_id = (
        _require_project_db_id(project)
        if project is not None
        else _normalize_positive_int(project_db_id, field_name="project_db_id")
    )
    if (role_key is None) == (role_id is None):
        raise ProjectAccessValidationError(
            "exactly one of role_key or role_id is required"
        )

    query = current_session.query(ProjectRole).filter(
        ProjectRole.project_db_id == resolved_project_db_id
    )
    if role_key is not None:
        query = query.filter(
            ProjectRole.role_key == _normalize_key(
                role_key,
                field_name="role_key",
            )
        )
    else:
        normalized_role_id = _normalize_required_public_id(
            role_id,
            field_name="role_id",
        )
        query = query.filter(ProjectRole.role_id == normalized_role_id)

    query = _filter_not_deleted(
        query,
        ProjectRole,
        include_deleted=include_deleted,
    )
    query = _with_optional_lock(query, lock=lock)
    result = _query_one_or_none(query)
    if result is None and required:
        raise ProjectAccessNotFoundError(
            "project role was not found",
            code="project_role_not_found",
            details={
                "projectDbId": resolved_project_db_id,
                "roleKey": role_key,
                "roleId": role_id,
            },
        )
    return result


def ensure_default_project_roles(
    *,
    project: Any,
    actor_user_id: Any = None,
    session: Any = None,
    synchronize_existing: bool = True,
    restore_deleted: bool = True,
    flush: bool = True,
) -> tuple[dict[str, ProjectRole], MutationStats]:
    """Erzeugt oder repariert die vier systemischen Standardrollen idempotent."""

    current_session = _resolve_session(session)
    project_db_id = _require_project_db_id(project)
    actor = _normalize_user_id(
        actor_user_id,
        field_name="actor_user_id",
        required=False,
    )
    templates = get_default_role_templates()
    existing_roles = list_project_roles(
        project_db_id=project_db_id,
        session=current_session,
        include_deleted=True,
        lock=True,
    )
    existing_by_key: dict[str, ProjectRole] = {}
    for role in existing_roles:
        key = _normalize_key(role.role_key, field_name="role.role_key")
        if key in existing_by_key:
            raise ProjectAccessInvariantError(
                "multiple project roles use the same role key",
                details={"projectDbId": project_db_id, "roleKey": key},
            )
        existing_by_key[key] = role

    result: dict[str, ProjectRole] = {}
    stats = MutationStats()
    pending: list[Any] = []

    for role_key in DEFAULT_PROJECT_ROLE_KEYS:
        template = templates[role_key]
        existing = existing_by_key.get(role_key)
        if existing is None:
            try:
                role = ProjectRole.create(
                    project_db_id=project_db_id,
                    role_key=role_key,
                    name=template.get("name"),
                    description=template.get("description"),
                    permissions=template.get("permissions"),
                    is_system=True,
                    status=ROLE_STATUS_ACTIVE,
                    created_by_user_id=actor,
                    metadata={
                        "source": "project-access-service",
                        "defaultRole": True,
                        "serviceVersion": PROJECT_ACCESS_SERVICE_VERSION,
                    },
                )
            except ValueError as exc:
                raise ProjectAccessValidationError(
                    "default project role could not be created",
                    details={"roleKey": role_key},
                    cause=exc,
                ) from exc
            current_session.add(role)
            pending.append(role)
            result[role_key] = role
            stats.created += 1
            continue

        changed = False
        reactivated = False
        if _is_deleted(existing):
            if not restore_deleted:
                raise ProjectAccessConflictError(
                    "default role exists but is deleted",
                    code="default_role_deleted",
                    details={"roleKey": role_key},
                )
            try:
                existing.restore(restored_by_user_id=actor)
            except Exception as exc:
                raise ProjectAccessInvariantError(
                    "deleted default role could not be restored",
                    details={"roleKey": role_key},
                    cause=exc,
                ) from exc
            reactivated = True
            changed = True

        if synchronize_existing:
            expected_name = str(template.get("name") or role_key.title())
            expected_description = template.get("description")
            expected_permissions = copy.deepcopy(template.get("permissions") or {})
            if existing.name != expected_name:
                existing.name = expected_name
                changed = True
            if existing.description != expected_description:
                existing.description = expected_description
                changed = True
            if existing.permissions_json != expected_permissions:
                existing.permissions_json = expected_permissions
                changed = True
            if not bool(existing.is_system):
                existing.is_system = True
                changed = True
            if existing.status != ROLE_STATUS_ACTIVE:
                existing.status = ROLE_STATUS_ACTIVE
                existing.deleted_at = None
                changed = True

        if changed:
            _mark_changed_once(existing, actor_user_id=actor)
            if reactivated:
                stats.reactivated += 1
            else:
                stats.updated += 1
            pending.append(existing)
        else:
            stats.reused += 1
        try:
            existing.validate_or_raise()
        except ValueError as exc:
            raise ProjectAccessInvariantError(
                "existing default role is invalid",
                details={"roleKey": role_key},
                cause=exc,
            ) from exc
        result[role_key] = existing

    if flush and pending:
        _flush_session(current_session, objects=pending)

    for role_key, role in result.items():
        if getattr(role, "id", None) is None and flush:
            raise ProjectAccessInvariantError(
                "default role did not receive an internal database id",
                details={"roleKey": role_key},
            )

    return result, stats


# ---------------------------------------------------------------------------
# Owner-Zuweisung
# ---------------------------------------------------------------------------


def _list_role_assignments_for_role(
    *,
    project_db_id: int,
    role: ProjectRole,
    session: Any,
    include_deleted: bool = True,
    lock: bool = False,
) -> list[ProjectRoleAssignment]:
    _require_same_project(
        project_db_id=project_db_id,
        entity=role,
        entity_name="role",
    )
    role_db_id = _normalize_positive_int(role.id, field_name="role.id")
    query = session.query(ProjectRoleAssignment).filter(
        ProjectRoleAssignment.project_db_id == project_db_id,
        ProjectRoleAssignment.role_db_id == role_db_id,
    )
    query = _filter_not_deleted(
        query,
        ProjectRoleAssignment,
        include_deleted=include_deleted,
    )
    query = _with_optional_lock(query, lock=lock)
    return _query_all(query)


def ensure_owner_role_assignment(
    *,
    project: Any,
    owner_user_id: Any,
    owner_role: Optional[ProjectRole] = None,
    actor_user_id: Any = None,
    session: Any = None,
    replace_existing_owner: bool = False,
    flush: bool = True,
) -> EntityMutationResult:
    """Stellt eine eindeutige aktive Owner-Zuweisung für den gewünschten User sicher."""

    current_session = _resolve_session(session)
    project_db_id = _require_project_db_id(project)
    owner = _normalize_user_id(
        owner_user_id,
        field_name="owner_user_id",
        required=True,
    )
    actor = _normalize_user_id(
        actor_user_id,
        field_name="actor_user_id",
        required=False,
    ) or owner

    role = owner_role or find_project_role(
        project_db_id=project_db_id,
        role_key=DEFAULT_ROLE_OWNER,
        session=current_session,
        include_deleted=False,
        lock=True,
        required=True,
    )
    assert role is not None
    _require_same_project(
        project_db_id=project_db_id,
        entity=role,
        entity_name="owner_role",
    )
    if role.role_key != DEFAULT_ROLE_OWNER:
        raise ProjectAccessInvariantError(
            "provided owner role does not use role_key=owner",
            details={"roleKey": role.role_key},
        )

    assignments = _list_role_assignments_for_role(
        project_db_id=project_db_id,
        role=role,
        session=current_session,
        include_deleted=True,
        lock=True,
    )
    target: Optional[ProjectRoleAssignment] = None
    conflicting: list[ProjectRoleAssignment] = []
    for assignment in assignments:
        if assignment.subject_type != SUBJECT_TYPE_USER:
            if assignment.status == ASSIGNMENT_STATUS_ACTIVE and not _is_deleted(assignment):
                conflicting.append(assignment)
            continue
        if assignment.user_id == owner:
            if target is not None:
                raise ProjectAccessInvariantError(
                    "duplicate owner assignments exist for the same user",
                    details={"ownerUserId": owner},
                )
            target = assignment
        elif assignment.status == ASSIGNMENT_STATUS_ACTIVE and not _is_deleted(assignment):
            conflicting.append(assignment)

    if conflicting and not replace_existing_owner:
        raise ProjectAccessConflictError(
            "another active owner assignment already exists",
            code="owner_assignment_conflict",
            details={
                "requestedOwnerUserId": owner,
                "conflictingAssignments": [
                    _serialize_entity(item, include_internal=True)
                    for item in conflicting
                ],
            },
        )

    stats = MutationStats()
    pending: list[Any] = []
    if conflicting and replace_existing_owner:
        for assignment in conflicting:
            try:
                assignment.revoke(
                    revoked_by_user_id=actor,
                    reason="owner replaced by project access initialization",
                )
            except ValueError as exc:
                raise ProjectAccessInvariantError(
                    "existing owner assignment could not be revoked",
                    details={"assignmentId": assignment.assignment_id},
                    cause=exc,
                ) from exc
            stats.revoked += 1
            pending.append(assignment)

    if target is None:
        try:
            target = ProjectRoleAssignment.create_for_user(
                project_db_id=project_db_id,
                role_db_id=role.id,
                role_id=role.role_id,
                user_id=owner,
                assigned_by_user_id=actor,
                status=ASSIGNMENT_STATUS_ACTIVE,
                metadata={
                    "source": "project-access-service",
                    "ownerAssignment": True,
                    "serviceVersion": PROJECT_ACCESS_SERVICE_VERSION,
                },
            )
        except ValueError as exc:
            raise ProjectAccessValidationError(
                "owner role assignment could not be created",
                details={"ownerUserId": owner},
                cause=exc,
            ) from exc
        current_session.add(target)
        pending.append(target)
        stats.created += 1
        action = "created"
    elif _is_deleted(target):
        try:
            target.restore(restored_by_user_id=actor)
            target.reactivate(reactivated_by_user_id=actor)
        except ValueError as exc:
            raise ProjectAccessInvariantError(
                "deleted owner assignment could not be restored",
                details={"assignmentId": target.assignment_id},
                cause=exc,
            ) from exc
        pending.append(target)
        stats.reactivated += 1
        action = "reactivated"
    elif target.status in {
        ASSIGNMENT_STATUS_REVOKED,
        ASSIGNMENT_STATUS_INACTIVE,
    }:
        try:
            target.reactivate(reactivated_by_user_id=actor)
        except ValueError as exc:
            raise ProjectAccessInvariantError(
                "owner assignment could not be reactivated",
                details={"assignmentId": target.assignment_id},
                cause=exc,
            ) from exc
        pending.append(target)
        stats.reactivated += 1
        action = "reactivated"
    else:
        stats.reused += 1
        action = "reused"

    if flush and pending:
        _flush_session(current_session, objects=pending)

    return EntityMutationResult(
        entity=target,
        action=action,
        stats=stats,
    )


def ensure_project_access_initialized(
    *,
    project: Any = None,
    project_db_id: Any = None,
    project_id: Any = None,
    external_app_project_id: Any = None,
    owner_user_id: Any = None,
    actor_user_id: Any = None,
    session: Any = None,
    synchronize_default_roles: bool = True,
    restore_deleted_roles: bool = True,
    replace_existing_owner: bool = False,
    allow_missing_owner: bool = False,
    lock_project: bool = True,
    flush: bool = True,
) -> ProjectAccessInitializationResult:
    """Initialisiert Standardrollen und optional die Owner-Zuweisung atomar im Caller."""

    current_session = _resolve_session(session)
    resolved_project = resolve_project(
        session=current_session,
        project=project,
        project_db_id=project_db_id,
        project_id=project_id,
        external_app_project_id=external_app_project_id,
        include_deleted=False,
        lock=lock_project,
    )

    owner_source = owner_user_id
    if owner_source is None:
        owner_source = getattr(resolved_project, "owner_id", None)
    if owner_source is None:
        owner_source = getattr(resolved_project, "created_by_user_id", None)

    normalized_owner = _normalize_user_id(
        owner_source,
        field_name="owner_user_id",
        required=not allow_missing_owner,
    )
    normalized_actor = _normalize_user_id(
        actor_user_id,
        field_name="actor_user_id",
        required=False,
    ) or normalized_owner

    roles_by_key, role_stats = ensure_default_project_roles(
        project=resolved_project,
        actor_user_id=normalized_actor,
        session=current_session,
        synchronize_existing=synchronize_default_roles,
        restore_deleted=restore_deleted_roles,
        flush=flush,
    )

    warnings: list[str] = []
    owner_assignment: Optional[ProjectRoleAssignment] = None
    assignment_stats = MutationStats()
    if normalized_owner is None:
        warnings.append(
            "owner assignment was skipped because no owner_user_id was available"
        )
        assignment_stats.skipped += 1
    else:
        owner_result = ensure_owner_role_assignment(
            project=resolved_project,
            owner_user_id=normalized_owner,
            owner_role=roles_by_key[DEFAULT_ROLE_OWNER],
            actor_user_id=normalized_actor,
            session=current_session,
            replace_existing_owner=replace_existing_owner,
            flush=flush,
        )
        owner_assignment = owner_result.entity
        assignment_stats.merge(owner_result.stats)
        warnings.extend(owner_result.warnings)

    return ProjectAccessInitializationResult(
        project=resolved_project,
        owner_user_id=normalized_owner,
        roles_by_key=roles_by_key,
        owner_assignment=owner_assignment,
        role_stats=role_stats,
        assignment_stats=assignment_stats,
        warnings=warnings,
    )


# ---------------------------------------------------------------------------
# Gruppen
# ---------------------------------------------------------------------------


def list_project_groups(
    *,
    project: Any = None,
    project_db_id: Any = None,
    session: Any = None,
    include_deleted: bool = False,
    lock: bool = False,
) -> list[ProjectGroup]:
    current_session = _resolve_session(session)
    resolved_project_db_id = (
        _require_project_db_id(project)
        if project is not None
        else _normalize_positive_int(project_db_id, field_name="project_db_id")
    )
    query = current_session.query(ProjectGroup).filter(
        ProjectGroup.project_db_id == resolved_project_db_id
    )
    query = _filter_not_deleted(
        query,
        ProjectGroup,
        include_deleted=include_deleted,
    )
    query = _with_optional_lock(query, lock=lock)
    try:
        query = query.order_by(ProjectGroup.group_key.asc(), ProjectGroup.id.asc())
    except Exception:
        pass
    return _query_all(query)


def find_project_group(
    *,
    project: Any = None,
    project_db_id: Any = None,
    group_key: Any = None,
    group_id: Any = None,
    session: Any = None,
    include_deleted: bool = False,
    lock: bool = False,
    required: bool = False,
) -> Optional[ProjectGroup]:
    current_session = _resolve_session(session)
    resolved_project_db_id = (
        _require_project_db_id(project)
        if project is not None
        else _normalize_positive_int(project_db_id, field_name="project_db_id")
    )
    if (group_key is None) == (group_id is None):
        raise ProjectAccessValidationError(
            "exactly one of group_key or group_id is required"
        )
    query = current_session.query(ProjectGroup).filter(
        ProjectGroup.project_db_id == resolved_project_db_id
    )
    if group_key is not None:
        query = query.filter(
            ProjectGroup.group_key == _normalize_key(
                group_key,
                field_name="group_key",
            )
        )
    else:
        normalized_group_id = _normalize_required_public_id(
            group_id,
            field_name="group_id",
        )
        query = query.filter(ProjectGroup.group_id == normalized_group_id)
    query = _filter_not_deleted(
        query,
        ProjectGroup,
        include_deleted=include_deleted,
    )
    query = _with_optional_lock(query, lock=lock)
    result = _query_one_or_none(query)
    if result is None and required:
        raise ProjectAccessNotFoundError(
            "project group was not found",
            code="project_group_not_found",
            details={
                "projectDbId": resolved_project_db_id,
                "groupKey": group_key,
                "groupId": group_id,
            },
        )
    return result


def ensure_project_group(
    *,
    project: Any,
    payload: Mapping[str, Any],
    actor_user_id: Any = None,
    session: Any = None,
    synchronize_existing: bool = True,
    restore_deleted: bool = True,
    flush: bool = True,
) -> EntityMutationResult:
    """Erzeugt eine Gruppe anhand ihres stabilen group_key idempotent."""

    if not isinstance(payload, Mapping):
        raise ProjectAccessValidationError("group payload must be an object")
    current_session = _resolve_session(session)
    project_db_id = _require_project_db_id(project)
    actor = _normalize_user_id(
        actor_user_id,
        field_name="actor_user_id",
        required=False,
    )
    raw_key = payload.get("groupKey", payload.get("group_key", payload.get("key")))
    group_key = _normalize_key(raw_key, field_name="group_key")
    requested_group_id = _normalize_optional_public_id(
        payload.get("groupId", payload.get("group_id")),
        field_name="group_id",
    )
    existing = find_project_group(
        project_db_id=project_db_id,
        group_key=group_key,
        session=current_session,
        include_deleted=True,
        lock=True,
        required=False,
    )

    stats = MutationStats()
    if existing is None:
        try:
            group = ProjectGroup.from_create_payload(
                project_db_id=project_db_id,
                payload=payload,
                created_by_user_id=actor,
            )
        except ValueError as exc:
            raise ProjectAccessValidationError(
                "project group could not be created",
                details={"groupKey": group_key},
                cause=exc,
            ) from exc
        current_session.add(group)
        stats.created += 1
        action = "created"
        if flush:
            _flush_session(current_session, objects=[group])
        return EntityMutationResult(entity=group, action=action, stats=stats)

    if requested_group_id and existing.group_id != requested_group_id:
        raise ProjectAccessConflictError(
            "group_key already exists with another group_id",
            code="project_group_identity_conflict",
            details={
                "groupKey": group_key,
                "existingGroupId": existing.group_id,
                "requestedGroupId": requested_group_id,
            },
        )

    changed = False
    if _is_deleted(existing):
        if not restore_deleted:
            raise ProjectAccessConflictError(
                "project group exists but is deleted",
                code="project_group_deleted",
                details={"groupKey": group_key},
            )
        existing.restore(restored_by_user_id=actor)
        stats.reactivated += 1
        changed = True
        action = "reactivated"
    else:
        action = "reused"

    if synchronize_existing:
        try:
            candidate = ProjectGroup.from_create_payload(
                project_db_id=project_db_id,
                payload=payload,
                created_by_user_id=actor,
            )
        except ValueError as exc:
            raise ProjectAccessValidationError(
                "project group update is invalid",
                details={"groupKey": group_key},
                cause=exc,
            ) from exc

        mutable_fields = (
            "name",
            "description",
            "is_system",
            "status",
            "metadata_json",
        )
        candidate_changed = any(
            getattr(existing, field_name, None) != getattr(candidate, field_name, None)
            for field_name in mutable_fields
        )
        if candidate_changed:
            for field_name in mutable_fields:
                setattr(existing, field_name, copy.deepcopy(getattr(candidate, field_name)))
            if existing.status != GROUP_STATUS_DELETED:
                existing.deleted_at = None
            _mark_changed_once(existing, actor_user_id=actor)
            try:
                existing.validate_or_raise()
            except ValueError as exc:
                raise ProjectAccessInvariantError(
                    "updated project group is invalid",
                    details={"groupKey": group_key},
                    cause=exc,
                ) from exc
            changed = True
            if stats.reactivated == 0:
                stats.updated += 1
            action = "updated" if action == "reused" else action

    if not changed:
        stats.reused += 1
    if flush and changed:
        _flush_session(current_session, objects=[existing])
    return EntityMutationResult(entity=existing, action=action, stats=stats)


# ---------------------------------------------------------------------------
# Gruppenmitgliedschaften
# ---------------------------------------------------------------------------


def list_group_memberships(
    *,
    project: Any,
    group: Optional[ProjectGroup] = None,
    group_id: Any = None,
    user_id: Any = None,
    session: Any = None,
    include_deleted: bool = False,
    lock: bool = False,
) -> list[ProjectGroupMember]:
    current_session = _resolve_session(session)
    project_db_id = _require_project_db_id(project)
    resolved_group = group
    if resolved_group is None and group_id is not None:
        resolved_group = find_project_group(
            project_db_id=project_db_id,
            group_id=group_id,
            session=current_session,
            include_deleted=include_deleted,
            required=True,
        )
    query = current_session.query(ProjectGroupMember).filter(
        ProjectGroupMember.project_db_id == project_db_id
    )
    if resolved_group is not None:
        _require_same_project(
            project_db_id=project_db_id,
            entity=resolved_group,
            entity_name="group",
        )
        query = query.filter(
            ProjectGroupMember.group_db_id == _normalize_positive_int(
                resolved_group.id,
                field_name="group.id",
            )
        )
    if user_id is not None:
        normalized_user = _normalize_user_id(
            user_id,
            field_name="user_id",
            required=True,
        )
        query = query.filter(ProjectGroupMember.user_id == normalized_user)
    query = _filter_not_deleted(
        query,
        ProjectGroupMember,
        include_deleted=include_deleted,
    )
    query = _with_optional_lock(query, lock=lock)
    return _query_all(query)


def ensure_user_in_group(
    *,
    project: Any,
    group: Optional[ProjectGroup] = None,
    group_id: Any = None,
    user_id: Any,
    actor_user_id: Any = None,
    starts_at: Any = None,
    expires_at: Any = None,
    metadata: Optional[Mapping[str, Any]] = None,
    session: Any = None,
    flush: bool = True,
) -> EntityMutationResult:
    """Fügt einen externen User idempotent zu einer Projektgruppe hinzu."""

    current_session = _resolve_session(session)
    project_db_id = _require_project_db_id(project)
    resolved_group = group or find_project_group(
        project_db_id=project_db_id,
        group_id=group_id,
        session=current_session,
        include_deleted=False,
        lock=True,
        required=True,
    )
    assert resolved_group is not None
    _require_same_project(
        project_db_id=project_db_id,
        entity=resolved_group,
        entity_name="group",
    )
    if _is_deleted(resolved_group):
        raise ProjectAccessConflictError(
            "cannot add a member to a deleted group",
            code="project_group_deleted",
        )
    normalized_user = _normalize_user_id(
        user_id,
        field_name="user_id",
        required=True,
    )
    actor = _normalize_user_id(
        actor_user_id,
        field_name="actor_user_id",
        required=False,
    )

    memberships = list_group_memberships(
        project=project,
        group=resolved_group,
        user_id=normalized_user,
        session=current_session,
        include_deleted=True,
        lock=True,
    )
    if len(memberships) > 1:
        raise ProjectAccessInvariantError(
            "duplicate group memberships exist",
            details={
                "groupId": resolved_group.group_id,
                "userId": normalized_user,
            },
        )
    existing = memberships[0] if memberships else None
    stats = MutationStats()

    if existing is None:
        try:
            membership = ProjectGroupMember.create(
                project_db_id=project_db_id,
                group_db_id=resolved_group.id,
                group_id=resolved_group.group_id,
                user_id=normalized_user,
                status=MEMBERSHIP_STATUS_ACTIVE,
                added_by_user_id=actor,
                starts_at=starts_at,
                expires_at=expires_at,
                metadata=dict(metadata or {}),
            )
        except ValueError as exc:
            raise ProjectAccessValidationError(
                "group membership could not be created",
                cause=exc,
            ) from exc
        current_session.add(membership)
        stats.created += 1
        if flush:
            _flush_session(current_session, objects=[membership])
        return EntityMutationResult(
            entity=membership,
            action="created",
            stats=stats,
        )

    if _is_deleted(existing):
        existing.restore(restored_by_user_id=actor)
        existing.reactivate(
            reactivated_by_user_id=actor,
            starts_at=starts_at,
            expires_at=expires_at,
        )
        stats.reactivated += 1
        action = "reactivated"
    elif existing.status in {
        MEMBERSHIP_STATUS_REMOVED,
        MEMBERSHIP_STATUS_INACTIVE,
    }:
        existing.reactivate(
            reactivated_by_user_id=actor,
            starts_at=starts_at,
            expires_at=expires_at,
        )
        stats.reactivated += 1
        action = "reactivated"
    else:
        stats.reused += 1
        action = "reused"

    if metadata is not None and existing.metadata_json != dict(metadata):
        existing.replace_metadata(
            dict(metadata),
            updated_by_user_id=actor,
        )
        if action == "reused":
            stats.reused -= 1
            stats.updated += 1
            action = "updated"

    if flush and stats.changed:
        _flush_session(current_session, objects=[existing])
    return EntityMutationResult(entity=existing, action=action, stats=stats)


def remove_user_from_group(
    *,
    project: Any,
    group: Optional[ProjectGroup] = None,
    group_id: Any = None,
    user_id: Any,
    actor_user_id: Any = None,
    reason: Any = None,
    session: Any = None,
    missing_ok: bool = True,
    flush: bool = True,
) -> EntityMutationResult:
    current_session = _resolve_session(session)
    project_db_id = _require_project_db_id(project)
    resolved_group = group or find_project_group(
        project_db_id=project_db_id,
        group_id=group_id,
        session=current_session,
        include_deleted=True,
        required=True,
    )
    assert resolved_group is not None
    _require_same_project(
        project_db_id=project_db_id,
        entity=resolved_group,
        entity_name="group",
    )
    normalized_user = _normalize_user_id(
        user_id,
        field_name="user_id",
        required=True,
    )
    actor = _normalize_user_id(
        actor_user_id,
        field_name="actor_user_id",
        required=False,
    )
    memberships = list_group_memberships(
        project=project,
        group=resolved_group,
        user_id=normalized_user,
        session=current_session,
        include_deleted=True,
        lock=True,
    )
    if not memberships:
        if not missing_ok:
            raise ProjectAccessNotFoundError(
                "group membership was not found",
                code="project_group_membership_not_found",
            )
        return EntityMutationResult(
            entity=None,
            action="missing",
            stats=MutationStats(skipped=1),
        )
    if len(memberships) > 1:
        raise ProjectAccessInvariantError("duplicate group memberships exist")
    membership = memberships[0]
    if _is_deleted(membership) or membership.status == MEMBERSHIP_STATUS_REMOVED:
        return EntityMutationResult(
            entity=membership,
            action="reused",
            stats=MutationStats(reused=1),
        )
    try:
        membership.remove(
            removed_by_user_id=actor,
            reason=_normalize_reason(reason),
        )
    except ValueError as exc:
        raise ProjectAccessValidationError(
            "group membership could not be removed",
            cause=exc,
        ) from exc
    if flush:
        _flush_session(current_session, objects=[membership])
    return EntityMutationResult(
        entity=membership,
        action="removed",
        stats=MutationStats(removed=1),
    )


# ---------------------------------------------------------------------------
# Allgemeine Rollenzuweisungen
# ---------------------------------------------------------------------------


def list_project_role_assignments(
    *,
    project: Any,
    role: Optional[ProjectRole] = None,
    group: Optional[ProjectGroup] = None,
    user_id: Any = None,
    subject_type: Optional[str] = None,
    session: Any = None,
    include_deleted: bool = False,
    lock: bool = False,
) -> list[ProjectRoleAssignment]:
    current_session = _resolve_session(session)
    project_db_id = _require_project_db_id(project)
    query = current_session.query(ProjectRoleAssignment).filter(
        ProjectRoleAssignment.project_db_id == project_db_id
    )
    if role is not None:
        _require_same_project(
            project_db_id=project_db_id,
            entity=role,
            entity_name="role",
        )
        query = query.filter(
            ProjectRoleAssignment.role_db_id == _normalize_positive_int(
                role.id,
                field_name="role.id",
            )
        )
    if group is not None:
        _require_same_project(
            project_db_id=project_db_id,
            entity=group,
            entity_name="group",
        )
        query = query.filter(
            ProjectRoleAssignment.subject_type == SUBJECT_TYPE_GROUP,
            ProjectRoleAssignment.group_db_id == _normalize_positive_int(
                group.id,
                field_name="group.id",
            ),
        )
    if user_id is not None:
        normalized_user = _normalize_user_id(
            user_id,
            field_name="user_id",
            required=True,
        )
        query = query.filter(
            ProjectRoleAssignment.subject_type == SUBJECT_TYPE_USER,
            ProjectRoleAssignment.user_id == normalized_user,
        )
    if subject_type is not None:
        normalized_subject_type = str(subject_type).strip().lower()
        if normalized_subject_type not in {SUBJECT_TYPE_USER, SUBJECT_TYPE_GROUP}:
            raise ProjectAccessValidationError(
                "subject_type must be user or group"
            )
        query = query.filter(
            ProjectRoleAssignment.subject_type == normalized_subject_type
        )
    query = _filter_not_deleted(
        query,
        ProjectRoleAssignment,
        include_deleted=include_deleted,
    )
    query = _with_optional_lock(query, lock=lock)
    return _query_all(query)


def ensure_role_assignment(
    *,
    project: Any,
    role: Optional[ProjectRole] = None,
    role_id: Any = None,
    role_key: Any = None,
    subject_type: str,
    user_id: Any = None,
    group: Optional[ProjectGroup] = None,
    group_id: Any = None,
    actor_user_id: Any = None,
    permission_overrides: Optional[Mapping[str, Any]] = None,
    starts_at: Any = None,
    expires_at: Any = None,
    metadata: Optional[Mapping[str, Any]] = None,
    session: Any = None,
    flush: bool = True,
) -> EntityMutationResult:
    """Erzeugt oder reaktiviert eine Rollenvergabe für User oder Gruppe."""

    current_session = _resolve_session(session)
    project_db_id = _require_project_db_id(project)
    normalized_subject_type = str(subject_type).strip().lower()
    if normalized_subject_type not in {SUBJECT_TYPE_USER, SUBJECT_TYPE_GROUP}:
        raise ProjectAccessValidationError(
            "subject_type must be user or group"
        )

    resolved_role = role
    if resolved_role is None:
        resolved_role = find_project_role(
            project_db_id=project_db_id,
            role_key=role_key,
            role_id=role_id,
            session=current_session,
            include_deleted=False,
            lock=True,
            required=True,
        )
    assert resolved_role is not None
    _require_same_project(
        project_db_id=project_db_id,
        entity=resolved_role,
        entity_name="role",
    )
    if _is_deleted(resolved_role):
        raise ProjectAccessConflictError(
            "cannot assign a deleted role",
            code="project_role_deleted",
        )

    normalized_user: Optional[str] = None
    resolved_group: Optional[ProjectGroup] = None
    if normalized_subject_type == SUBJECT_TYPE_USER:
        normalized_user = _normalize_user_id(
            user_id,
            field_name="user_id",
            required=True,
        )
        if group is not None or group_id is not None:
            raise ProjectAccessValidationError(
                "group must not be supplied for a user assignment"
            )
    else:
        if user_id is not None:
            raise ProjectAccessValidationError(
                "user_id must not be supplied for a group assignment"
            )
        resolved_group = group or find_project_group(
            project_db_id=project_db_id,
            group_id=group_id,
            session=current_session,
            include_deleted=False,
            lock=True,
            required=True,
        )
        assert resolved_group is not None
        _require_same_project(
            project_db_id=project_db_id,
            entity=resolved_group,
            entity_name="group",
        )
        if _is_deleted(resolved_group):
            raise ProjectAccessConflictError(
                "cannot assign a role to a deleted group",
                code="project_group_deleted",
            )

    actor = _normalize_user_id(
        actor_user_id,
        field_name="actor_user_id",
        required=False,
    )
    assignments = list_project_role_assignments(
        project=project,
        role=resolved_role,
        user_id=normalized_user,
        group=resolved_group,
        session=current_session,
        include_deleted=True,
        lock=True,
    )
    if len(assignments) > 1:
        raise ProjectAccessInvariantError(
            "duplicate role assignments exist for subject and role"
        )
    existing = assignments[0] if assignments else None
    stats = MutationStats()

    if existing is None:
        try:
            if normalized_subject_type == SUBJECT_TYPE_USER:
                assignment = ProjectRoleAssignment.create_for_user(
                    project_db_id=project_db_id,
                    role_db_id=resolved_role.id,
                    role_id=resolved_role.role_id,
                    user_id=normalized_user,
                    permission_overrides=dict(permission_overrides or {}),
                    status=ASSIGNMENT_STATUS_ACTIVE,
                    assigned_by_user_id=actor,
                    starts_at=starts_at,
                    expires_at=expires_at,
                    metadata=dict(metadata or {}),
                )
            else:
                assert resolved_group is not None
                assignment = ProjectRoleAssignment.create_for_group(
                    project_db_id=project_db_id,
                    role_db_id=resolved_role.id,
                    role_id=resolved_role.role_id,
                    group_db_id=resolved_group.id,
                    group_id=resolved_group.group_id,
                    permission_overrides=dict(permission_overrides or {}),
                    status=ASSIGNMENT_STATUS_ACTIVE,
                    assigned_by_user_id=actor,
                    starts_at=starts_at,
                    expires_at=expires_at,
                    metadata=dict(metadata or {}),
                )
        except ValueError as exc:
            raise ProjectAccessValidationError(
                "role assignment could not be created",
                cause=exc,
            ) from exc
        current_session.add(assignment)
        stats.created += 1
        if flush:
            _flush_session(current_session, objects=[assignment])
        return EntityMutationResult(
            entity=assignment,
            action="created",
            stats=stats,
        )

    if _is_deleted(existing):
        existing.restore(restored_by_user_id=actor)
        existing.reactivate(
            reactivated_by_user_id=actor,
            starts_at=starts_at,
            expires_at=expires_at,
        )
        stats.reactivated += 1
        action = "reactivated"
    elif existing.status in {
        ASSIGNMENT_STATUS_REVOKED,
        ASSIGNMENT_STATUS_INACTIVE,
    }:
        existing.reactivate(
            reactivated_by_user_id=actor,
            starts_at=starts_at,
            expires_at=expires_at,
        )
        stats.reactivated += 1
        action = "reactivated"
    else:
        stats.reused += 1
        action = "reused"

    if (
        permission_overrides is not None
        and existing.permission_overrides_json != dict(permission_overrides)
    ):
        existing.set_permission_overrides(
            dict(permission_overrides),
            updated_by_user_id=actor,
        )
        if action == "reused":
            stats.reused -= 1
            stats.updated += 1
            action = "updated"
    if metadata is not None and existing.metadata_json != dict(metadata):
        existing.replace_metadata(
            dict(metadata),
            updated_by_user_id=actor,
        )
        if action == "reused":
            stats.reused -= 1
            stats.updated += 1
            action = "updated"

    if flush and stats.changed:
        _flush_session(current_session, objects=[existing])
    return EntityMutationResult(entity=existing, action=action, stats=stats)


def assign_role_to_user(**kwargs: Any) -> EntityMutationResult:
    kwargs["subject_type"] = SUBJECT_TYPE_USER
    return ensure_role_assignment(**kwargs)


def assign_role_to_group(**kwargs: Any) -> EntityMutationResult:
    kwargs["subject_type"] = SUBJECT_TYPE_GROUP
    return ensure_role_assignment(**kwargs)


def revoke_role_assignment(
    *,
    project: Any,
    assignment_id: Any,
    actor_user_id: Any = None,
    reason: Any = None,
    session: Any = None,
    missing_ok: bool = False,
    flush: bool = True,
) -> EntityMutationResult:
    current_session = _resolve_session(session)
    project_db_id = _require_project_db_id(project)
    normalized_assignment_id = _normalize_required_public_id(
        assignment_id,
        field_name="assignment_id",
    )
    query = current_session.query(ProjectRoleAssignment).filter(
        ProjectRoleAssignment.project_db_id == project_db_id,
        ProjectRoleAssignment.assignment_id == normalized_assignment_id,
    )
    query = _with_optional_lock(query, lock=True)
    assignment = _query_one_or_none(query)
    if assignment is None:
        if missing_ok:
            return EntityMutationResult(
                entity=None,
                action="missing",
                stats=MutationStats(skipped=1),
            )
        raise ProjectAccessNotFoundError(
            "role assignment was not found",
            code="project_role_assignment_not_found",
        )
    if _is_deleted(assignment) or assignment.status == ASSIGNMENT_STATUS_REVOKED:
        return EntityMutationResult(
            entity=assignment,
            action="reused",
            stats=MutationStats(reused=1),
        )
    actor = _normalize_user_id(
        actor_user_id,
        field_name="actor_user_id",
        required=False,
    )
    try:
        assignment.revoke(
            revoked_by_user_id=actor,
            reason=_normalize_reason(reason),
        )
    except ValueError as exc:
        raise ProjectAccessValidationError(
            "role assignment could not be revoked",
            cause=exc,
        ) from exc
    if flush:
        _flush_session(current_session, objects=[assignment])
    return EntityMutationResult(
        entity=assignment,
        action="revoked",
        stats=MutationStats(revoked=1),
    )


# ---------------------------------------------------------------------------
# Projektweites Soft-Delete und Zusammenfassung
# ---------------------------------------------------------------------------


def soft_delete_project_access(
    *,
    project: Any,
    actor_user_id: Any = None,
    session: Any = None,
    flush: bool = True,
) -> ProjectAccessDeleteResult:
    """Soft-löscht sämtliche Access-Zeilen eines Projekts idempotent."""

    current_session = _resolve_session(session)
    project_db_id = _require_project_db_id(project)
    actor = _normalize_user_id(
        actor_user_id,
        field_name="actor_user_id",
        required=False,
    )

    assignments = list_project_role_assignments(
        project=project,
        session=current_session,
        include_deleted=True,
        lock=True,
    )
    memberships = list_group_memberships(
        project=project,
        session=current_session,
        include_deleted=True,
        lock=True,
    )
    groups = list_project_groups(
        project_db_id=project_db_id,
        session=current_session,
        include_deleted=True,
        lock=True,
    )
    roles = list_project_roles(
        project_db_id=project_db_id,
        session=current_session,
        include_deleted=True,
        lock=True,
    )

    stats = MutationStats()
    pending: list[Any] = []
    for entity in [*assignments, *memberships, *groups, *roles]:
        if _is_deleted(entity):
            stats.reused += 1
            continue
        try:
            entity.soft_delete(deleted_by_user_id=actor)
        except Exception as exc:
            raise ProjectAccessInvariantError(
                "project access entity could not be soft-deleted",
                details={
                    "entityType": type(entity).__name__,
                    "entityId": getattr(entity, "id", None),
                },
                cause=exc,
            ) from exc
        stats.deleted += 1
        pending.append(entity)

    if flush and pending:
        _flush_session(current_session, objects=pending)

    return ProjectAccessDeleteResult(
        project=project,
        stats=stats,
        role_count=len(roles),
        group_count=len(groups),
        membership_count=len(memberships),
        assignment_count=len(assignments),
    )


def build_project_access_summary(
    *,
    project: Any,
    session: Any = None,
    include_deleted: bool = False,
    include_internal: bool = False,
    include_metadata: bool = True,
) -> dict[str, Any]:
    """Liest eine flache Access-Zusammenfassung ohne Rechteauswertung."""

    current_session = _resolve_session(session)
    project_db_id = _require_project_db_id(project)
    roles = list_project_roles(
        project_db_id=project_db_id,
        session=current_session,
        include_deleted=include_deleted,
    )
    groups = list_project_groups(
        project_db_id=project_db_id,
        session=current_session,
        include_deleted=include_deleted,
    )
    memberships = list_group_memberships(
        project=project,
        session=current_session,
        include_deleted=include_deleted,
    )
    assignments = list_project_role_assignments(
        project=project,
        session=current_session,
        include_deleted=include_deleted,
    )

    role_keys = {role.role_key for role in roles if not _is_deleted(role)}
    owner_assignments = [
        assignment
        for assignment in assignments
        if assignment.role_id in {
            role.role_id for role in roles if role.role_key == DEFAULT_ROLE_OWNER
        }
        and assignment.status == ASSIGNMENT_STATUS_ACTIVE
        and not _is_deleted(assignment)
    ]
    return {
        "ok": True,
        "responseVersion": "project-access-summary-response.v1",
        "serviceVersion": PROJECT_ACCESS_SERVICE_VERSION,
        "projectId": getattr(project, "project_id", None),
        "projectDbId": project_db_id if include_internal else None,
        "accessInitialized": all(
            key in role_keys for key in DEFAULT_PROJECT_ROLE_KEYS
        ) and len(owner_assignments) >= 1,
        "authzEnforced": False,
        "effectivePermissionsCalculated": False,
        "counts": {
            "roles": len(roles),
            "groups": len(groups),
            "memberships": len(memberships),
            "assignments": len(assignments),
            "activeOwnerAssignments": len(owner_assignments),
        },
        "roles": [
            _serialize_entity(
                role,
                include_internal=include_internal,
                include_metadata=include_metadata,
            )
            for role in roles
        ],
        "groups": [
            _serialize_entity(
                group,
                include_internal=include_internal,
                include_metadata=include_metadata,
            )
            for group in groups
        ],
        "memberships": [
            _serialize_entity(
                membership,
                include_internal=include_internal,
                include_metadata=include_metadata,
            )
            for membership in memberships
        ],
        "assignments": [
            _serialize_entity(
                assignment,
                include_internal=include_internal,
                include_metadata=include_metadata,
            )
            for assignment in assignments
        ],
    }


# ---------------------------------------------------------------------------
# Objektorientierte Fassade für Routes/Provisioning
# ---------------------------------------------------------------------------


class ProjectAccessService:
    """Sessiongebundene Fassade über die transaktionsneutralen Funktionen."""

    def __init__(self, *, session: Any = None) -> None:
        self.session = _resolve_session(session)

    def resolve_project(self, **kwargs: Any) -> Any:
        kwargs.setdefault("session", self.session)
        return resolve_project(**kwargs)

    def initialize(self, **kwargs: Any) -> ProjectAccessInitializationResult:
        kwargs.setdefault("session", self.session)
        return ensure_project_access_initialized(**kwargs)

    def ensure_default_roles(
        self,
        **kwargs: Any,
    ) -> tuple[dict[str, ProjectRole], MutationStats]:
        kwargs.setdefault("session", self.session)
        return ensure_default_project_roles(**kwargs)

    def ensure_owner_assignment(
        self,
        **kwargs: Any,
    ) -> EntityMutationResult:
        kwargs.setdefault("session", self.session)
        return ensure_owner_role_assignment(**kwargs)

    def ensure_group(self, **kwargs: Any) -> EntityMutationResult:
        kwargs.setdefault("session", self.session)
        return ensure_project_group(**kwargs)

    def add_group_member(self, **kwargs: Any) -> EntityMutationResult:
        kwargs.setdefault("session", self.session)
        return ensure_user_in_group(**kwargs)

    def remove_group_member(self, **kwargs: Any) -> EntityMutationResult:
        kwargs.setdefault("session", self.session)
        return remove_user_from_group(**kwargs)

    def assign_role(self, **kwargs: Any) -> EntityMutationResult:
        kwargs.setdefault("session", self.session)
        return ensure_role_assignment(**kwargs)

    def assign_role_to_user(self, **kwargs: Any) -> EntityMutationResult:
        kwargs.setdefault("session", self.session)
        return assign_role_to_user(**kwargs)

    def assign_role_to_group(self, **kwargs: Any) -> EntityMutationResult:
        kwargs.setdefault("session", self.session)
        return assign_role_to_group(**kwargs)

    def revoke_assignment(self, **kwargs: Any) -> EntityMutationResult:
        kwargs.setdefault("session", self.session)
        return revoke_role_assignment(**kwargs)

    def soft_delete(self, **kwargs: Any) -> ProjectAccessDeleteResult:
        kwargs.setdefault("session", self.session)
        return soft_delete_project_access(**kwargs)

    def summary(self, **kwargs: Any) -> dict[str, Any]:
        kwargs.setdefault("session", self.session)
        return build_project_access_summary(**kwargs)


# ---------------------------------------------------------------------------
# Diagnosevertrag
# ---------------------------------------------------------------------------


def get_project_access_service_contract() -> dict[str, Any]:
    """Liefert einen DB-freien Vertrag für Status- und Bootstrapdiagnose."""

    try:
        default_roles = sorted(get_default_role_templates())
        defaults_ready = set(DEFAULT_PROJECT_ROLE_KEYS).issubset(default_roles)
        cache_error = None
    except Exception as exc:
        default_roles = []
        defaults_ready = False
        cache_error = f"{type(exc).__name__}: {exc}"

    return {
        "serviceVersion": PROJECT_ACCESS_SERVICE_VERSION,
        "schemaVersion": PROJECT_ACCESS_SERVICE_SCHEMA_VERSION,
        "ready": defaults_ready,
        "defaultRolesReady": defaults_ready,
        "defaultRoleKeys": default_roles,
        "ownerRoleKey": DEFAULT_ROLE_OWNER,
        "supportedSubjectTypes": [SUBJECT_TYPE_USER, SUBJECT_TYPE_GROUP],
        "transactionNeutral": True,
        "commitsInternally": False,
        "rollbacksInternally": False,
        "flushSupported": True,
        "authzEnforced": False,
        "effectivePermissionsCalculated": False,
        "externalUserForeignKeys": False,
        "ormStateCached": False,
        "pureCaches": {
            "key": _normalize_key_cached.cache_info()._asdict(),
            "userId": _normalize_user_id_cached.cache_info()._asdict(),
            "defaultRoleTemplates": _default_role_template_cache.cache_info()._asdict(),
        },
        "cacheError": cache_error,
        "operations": [
            "initialize",
            "ensure_default_roles",
            "ensure_owner_assignment",
            "ensure_group",
            "add_group_member",
            "remove_group_member",
            "assign_role_to_user",
            "assign_role_to_group",
            "revoke_assignment",
            "soft_delete",
            "summary",
        ],
    }


__all__ = [
    "PROJECT_ACCESS_SERVICE_VERSION",
    "PROJECT_ACCESS_SERVICE_SCHEMA_VERSION",
    "ProjectAccessServiceError",
    "ProjectAccessValidationError",
    "ProjectAccessNotFoundError",
    "ProjectAccessConflictError",
    "ProjectAccessCrossProjectError",
    "ProjectAccessInvariantError",
    "ProjectAccessPersistenceError",
    "MutationStats",
    "EntityMutationResult",
    "ProjectAccessInitializationResult",
    "ProjectAccessDeleteResult",
    "ProjectAccessService",
    "get_default_role_templates",
    "clear_project_access_service_caches",
    "resolve_project",
    "list_project_roles",
    "find_project_role",
    "ensure_default_project_roles",
    "ensure_owner_role_assignment",
    "ensure_project_access_initialized",
    "list_project_groups",
    "find_project_group",
    "ensure_project_group",
    "list_group_memberships",
    "ensure_user_in_group",
    "remove_user_from_group",
    "list_project_role_assignments",
    "ensure_role_assignment",
    "assign_role_to_user",
    "assign_role_to_group",
    "revoke_role_assignment",
    "soft_delete_project_access",
    "build_project_access_summary",
    "get_project_access_service_contract",
]
