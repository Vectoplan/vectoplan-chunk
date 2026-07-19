# services/vectoplan-chunk/src/services/project_access_service.py
"""Project-scoped access projection and authorization for ``vectoplan-chunk``.

The app service owns project membership and roles.  This module stores and
verifies a synchronized projection inside the Chunk service.  It intentionally
contains no HTTP client and never calls ``vectoplan-app``.

Core invariants
---------------

* Only the canonical opaque ``auth_user_id`` crosses service boundaries.
* Local AppUser IDs, e-mail addresses, account IDs and request supplied actor
  overrides are rejected.
* Roles map one-to-one: ``owner``, ``admin``, ``editor`` and ``viewer``.
* ``viewer`` is read-only and cannot execute commands, materialize chunks or
  mutate project/world/access state.
* Existing group assignments are preserved during direct-user reconciliation.
* Owner replacement requires the dedicated owner-transfer operation.
* The app remains the source of truth.  Chunk stores an idempotent projection.
* Runtime business mutations are independent from schema/bootstrap mutations.

Persistence is repository based.  Routes may inject an application repository,
a SQLAlchemy model adapter or the included in-memory repository for tests.
"""

from __future__ import annotations

import copy
import hashlib
import hmac
import importlib
import json
import re
import threading
import time
from collections import OrderedDict
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import Any, Callable, ContextManager, Iterable, Iterator, Mapping, MutableMapping, Optional, Sequence


PROJECT_ACCESS_SERVICE_VERSION = "1.0.0"
PROJECT_ACCESS_SCHEMA_VERSION = "project-access-v1"
PROJECT_ACCESS_PROJECTION_SCHEMA_VERSION = "project-access-projection-v1"
PROJECT_ACCESS_PLAN_SCHEMA_VERSION = "project-access-plan-v1"
PROJECT_ACCESS_RESULT_SCHEMA_VERSION = "project-access-result-v1"
PROJECT_ACCESS_DECISION_SCHEMA_VERSION = "project-access-decision-v1"
PROJECT_OWNER_TRANSFER_SCHEMA_VERSION = "project-owner-transfer-v1"

ROLE_OWNER = "owner"
ROLE_ADMIN = "admin"
ROLE_EDITOR = "editor"
ROLE_VIEWER = "viewer"
ACCESS_ROLES = (ROLE_OWNER, ROLE_ADMIN, ROLE_EDITOR, ROLE_VIEWER)
ROLE_RANK = {
    ROLE_VIEWER: 10,
    ROLE_EDITOR: 20,
    ROLE_ADMIN: 30,
    ROLE_OWNER: 40,
}

ASSIGNMENT_TYPE_DIRECT = "direct"
ASSIGNMENT_TYPE_GROUP = "group"
ASSIGNMENT_TYPES = (ASSIGNMENT_TYPE_DIRECT, ASSIGNMENT_TYPE_GROUP)

SYNC_STATUS_READY = "ready"
SYNC_STATUS_SYNCING = "syncing"
SYNC_STATUS_PENDING = "pending"
SYNC_STATUS_FAILED = "failed"
SYNC_STATUS_REPAIR_REQUIRED = "repair_required"
SYNC_STATUS_DISABLED = "disabled"
SYNC_STATUSES = (
    SYNC_STATUS_READY,
    SYNC_STATUS_SYNCING,
    SYNC_STATUS_PENDING,
    SYNC_STATUS_FAILED,
    SYNC_STATUS_REPAIR_REQUIRED,
    SYNC_STATUS_DISABLED,
)

CHANGE_ADD = "add"
CHANGE_UPDATE = "update"
CHANGE_REMOVE = "remove"
CHANGE_KEEP = "keep"
CHANGE_TRANSFER_OWNER = "transfer_owner"

OP_PROJECT_READ = "project.read"
OP_WORLD_READ = "world.read"
OP_BLOCKS_READ = "blocks.read"
OP_CHUNKS_READ = "chunks.read"
OP_CHUNKS_BATCH_READ = "chunks.batch.read"
OP_COMMANDS_EXECUTE = "commands.execute"
OP_CHUNKS_MATERIALIZE = "chunks.materialize"
OP_CHUNKS_WRITE = "chunks.write"
OP_PROJECT_MANAGE = "project.manage"
OP_ACCESS_MANAGE = "access.manage"
OP_WORLD_MUTATE = "world.mutate"
OP_OWNER_TRANSFER = "access.transfer_owner"

READ_OPERATIONS = frozenset(
    {
        OP_PROJECT_READ,
        OP_WORLD_READ,
        OP_BLOCKS_READ,
        OP_CHUNKS_READ,
        OP_CHUNKS_BATCH_READ,
    }
)
MUTATION_OPERATIONS = frozenset(
    {
        OP_COMMANDS_EXECUTE,
        OP_CHUNKS_MATERIALIZE,
        OP_CHUNKS_WRITE,
        OP_PROJECT_MANAGE,
        OP_ACCESS_MANAGE,
        OP_WORLD_MUTATE,
        OP_OWNER_TRANSFER,
    }
)
ALL_OPERATIONS = READ_OPERATIONS | MUTATION_OPERATIONS

DEFAULT_VIEWER_ALLOWED_OPERATIONS = tuple(sorted(READ_OPERATIONS))
DEFAULT_VIEWER_DENIED_OPERATIONS = tuple(sorted(MUTATION_OPERATIONS))
DEFAULT_ROLE_CAPABILITIES: dict[str, tuple[str, ...]] = {
    ROLE_OWNER: (
        "view",
        "edit",
        "command",
        "materialize",
        "manage",
        "manage_access",
        "transfer_owner",
    ),
    ROLE_ADMIN: ("view", "edit", "command", "materialize", "manage", "manage_access"),
    ROLE_EDITOR: ("view", "edit", "command", "materialize"),
    ROLE_VIEWER: ("view",),
}

CODE_OK = "project_access_ready"
CODE_DISABLED = "project_access_disabled"
CODE_INVALID_PROJECT = "chunk_project_id_invalid"
CODE_INVALID_PROJECTION = "project_access_projection_invalid"
CODE_CANONICAL_ID_REQUIRED = "canonical_auth_user_id_required"
CODE_LOCAL_ID_REJECTED = "local_user_id_not_allowed"
CODE_IDENTITY_OVERRIDE_DENIED = "identity_override_denied"
CODE_ROLE_INVALID = "project_role_invalid"
CODE_OWNER_REQUIRED = "project_owner_required"
CODE_MULTIPLE_OWNERS = "multiple_project_owners"
CODE_OWNER_TRANSFER_REQUIRED = "owner_transfer_required"
CODE_OWNER_TRANSFER_INVALID = "owner_transfer_invalid"
CODE_ASSIGNMENT_LIMIT = "project_access_assignment_limit_exceeded"
CODE_DUPLICATE_ASSIGNMENT = "duplicate_project_access_assignment"
CODE_REPOSITORY_REQUIRED = "project_access_repository_required"
CODE_REPOSITORY_ERROR = "project_access_repository_error"
CODE_VERIFICATION_FAILED = "project_access_verification_failed"
CODE_SERVICE_AUTH_REQUIRED = "service_authentication_required"
CODE_SERVICE_NOT_ALLOWED = "service_not_allowed_for_project_access"
CODE_ACCESS_DENIED = "project_access_denied"
CODE_PUBLIC_MUTATION_DENIED = "public_project_mutation_denied"
CODE_PUBLIC_READ_NOT_VERIFIED = "public_read_only_not_verified"
CODE_OPERATION_INVALID = "project_operation_invalid"
CODE_PROJECTION_STALE = "project_access_projection_stale"
CODE_INTERNAL_ERROR = "project_access_internal_error"

_DEFAULT_SOURCE_SERVICE = "vectoplan-app"
_DEFAULT_PROJECTION_VERSION = "app-project-access-v1"
_DEFAULT_CANONICAL_ID_FIELD = "auth_user_id"
_DEFAULT_MUTATION_SERVICES = ("vectoplan-app", "vectoplan-chunk-init")
_DEFAULT_DECISION_SERVICES = ("vectoplan-app", "vectoplan-editor", "vectoplan-chunk-init")

_CANONICAL_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,254}$")
_PROJECT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,254}$")
_OPERATION_RE = re.compile(r"^[a-z][a-z0-9_.:-]{1,119}$")
_EMAIL_RE = re.compile(r"(?i)(?<![\w.+-])([A-Z0-9._%+-]+)@([A-Z0-9.-]+\.[A-Z]{2,})(?![\w.-])")
_URL_RE = re.compile(r"(?i)\b(?:https?|postgres(?:ql)?|mysql|redis|amqp)://[^\s]+")
_SECRET_KEY_RE = re.compile(
    r"(?i)(?:^|_)(?:authorization|bearer|token|secret|password|passwd|cookie|session|api_key|apikey|private_key|credential)(?:$|_)"
)
_LOCAL_ID_KEY_RE = re.compile(
    r"(?i)^(?:user_id|userid|app_user_id|appuserid|local_user_id|localuserid|account_id|accountid|owner_user_id|owneruserid|email)$"
)
_CANONICAL_ID_KEYS = {
    "auth_user_id",
    "authuserid",
    "subject_auth_user_id",
    "subjectauthuserid",
}

_OPERATION_ALIASES: dict[str, str] = {
    "project": OP_PROJECT_READ,
    "project.view": OP_PROJECT_READ,
    "project.get": OP_PROJECT_READ,
    "read_project": OP_PROJECT_READ,
    "world": OP_WORLD_READ,
    "world.view": OP_WORLD_READ,
    "world.get": OP_WORLD_READ,
    "read_world": OP_WORLD_READ,
    "blocks": OP_BLOCKS_READ,
    "blocks.view": OP_BLOCKS_READ,
    "blocks.get": OP_BLOCKS_READ,
    "read_blocks": OP_BLOCKS_READ,
    "chunks": OP_CHUNKS_READ,
    "chunks.view": OP_CHUNKS_READ,
    "chunks.get": OP_CHUNKS_READ,
    "read_chunks": OP_CHUNKS_READ,
    "batch": OP_CHUNKS_BATCH_READ,
    "chunks.batch": OP_CHUNKS_BATCH_READ,
    "batch.read": OP_CHUNKS_BATCH_READ,
    "command": OP_COMMANDS_EXECUTE,
    "commands": OP_COMMANDS_EXECUTE,
    "execute": OP_COMMANDS_EXECUTE,
    "command.execute": OP_COMMANDS_EXECUTE,
    "materialize": OP_CHUNKS_MATERIALIZE,
    "chunk.materialize": OP_CHUNKS_MATERIALIZE,
    "chunks.create": OP_CHUNKS_MATERIALIZE,
    "chunk.write": OP_CHUNKS_WRITE,
    "write_chunks": OP_CHUNKS_WRITE,
    "project.update": OP_PROJECT_MANAGE,
    "project.delete": OP_PROJECT_MANAGE,
    "manage_project": OP_PROJECT_MANAGE,
    "access": OP_ACCESS_MANAGE,
    "access.update": OP_ACCESS_MANAGE,
    "assignments.manage": OP_ACCESS_MANAGE,
    "manage_access": OP_ACCESS_MANAGE,
    "world.write": OP_WORLD_MUTATE,
    "world.update": OP_WORLD_MUTATE,
    "mutate_world": OP_WORLD_MUTATE,
    "owner.transfer": OP_OWNER_TRANSFER,
    "transfer_owner": OP_OWNER_TRANSFER,
}

_ROLE_ALIASES: dict[str, str] = {
    "owner": ROLE_OWNER,
    "project_owner": ROLE_OWNER,
    "admin": ROLE_ADMIN,
    "administrator": ROLE_ADMIN,
    "project_admin": ROLE_ADMIN,
    "editor": ROLE_EDITOR,
    "write": ROLE_EDITOR,
    "writer": ROLE_EDITOR,
    "member": ROLE_EDITOR,
    "viewer": ROLE_VIEWER,
    "read": ROLE_VIEWER,
    "reader": ROLE_VIEWER,
    "readonly": ROLE_VIEWER,
    "read_only": ROLE_VIEWER,
}


# ---------------------------------------------------------------------------
# Generic helpers
# ---------------------------------------------------------------------------


def _utcnow_iso() -> str:
    try:
        return datetime.now(timezone.utc).isoformat()
    except Exception:
        return ""


def _safe_text(value: Any, default: str = "", max_len: int = 1000) -> str:
    try:
        if value is None:
            return default
        text = str(value).strip()
        if not text:
            return default
        text = "".join(character for character in text if character >= " " and character != "\x7f")
        return text[: max(0, int(max_len))] or default
    except Exception:
        return default


def _safe_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return bool(value)
    text = _safe_text(value, "", 32).lower()
    if text in {"1", "true", "yes", "on", "enabled", "active", "ready"}:
        return True
    if text in {"0", "false", "no", "off", "disabled", "inactive", "none", "null", ""}:
        return False
    return default


def _safe_int(value: Any, default: int = 0, minimum: Optional[int] = None, maximum: Optional[int] = None) -> int:
    try:
        result = int(value)
    except (TypeError, ValueError):
        result = default
    if minimum is not None:
        result = max(minimum, result)
    if maximum is not None:
        result = min(maximum, result)
    return result


def _as_mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    return {}


def _as_sequence(value: Any) -> list[Any]:
    if value is None or isinstance(value, (str, bytes, bytearray, Mapping)):
        return []
    try:
        return list(value)
    except Exception:
        return []


def _get_value(source: Any, *names: str, default: Any = None) -> Any:
    for name in names:
        try:
            if isinstance(source, Mapping) and name in source:
                return source.get(name)
            if source is not None and hasattr(source, name):
                return getattr(source, name)
        except Exception:
            continue
    return default


def _set_value(target: Any, name: str, value: Any) -> bool:
    try:
        if isinstance(target, MutableMapping):
            target[name] = value
            return True
        setattr(target, name, value)
        return True
    except Exception:
        return False


def _hash_text(value: Any) -> str:
    text = _safe_text(value, "", 65536)
    if not text:
        return ""
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()


def _short_fingerprint(value: Any, prefix: str = "usr") -> str:
    digest = _hash_text(value)
    return f"{prefix}_{digest[:16]}" if digest else ""


def _constant_equal(left: Any, right: Any) -> bool:
    try:
        return hmac.compare_digest(str(left), str(right))
    except Exception:
        return False


def redact_access_text(value: Any, *, max_len: int = 2000) -> str:
    text = _safe_text(value, "", max_len)
    if not text:
        return ""
    text = _EMAIL_RE.sub("[redacted-email]", text)
    text = _URL_RE.sub("[redacted-url]", text)
    return text[:max_len]


def sanitize_access_value(value: Any, *, depth: int = 0, max_depth: int = 5) -> Any:
    if depth > max_depth:
        return "[truncated]"
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return redact_access_text(value, max_len=2000)
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for raw_key, raw_value in list(value.items())[:128]:
            key = _safe_text(raw_key, "", 120)
            if not key:
                continue
            normalized = key.lower().replace("-", "_")
            if _SECRET_KEY_RE.search(normalized):
                result[key] = "[redacted]"
                continue
            if normalized in _CANONICAL_ID_KEYS or _LOCAL_ID_KEY_RE.match(normalized):
                result[key] = _short_fingerprint(raw_value)
                continue
            result[key] = sanitize_access_value(raw_value, depth=depth + 1, max_depth=max_depth)
        return result
    if isinstance(value, (list, tuple, set, frozenset)):
        return [sanitize_access_value(item, depth=depth + 1, max_depth=max_depth) for item in list(value)[:128]]
    return redact_access_text(value, max_len=500)


def sanitize_access_mapping(value: Any) -> dict[str, Any]:
    result = sanitize_access_value(value)
    return result if isinstance(result, dict) else {}


def _reject_local_identity_fields(value: Any, *, path: tuple[str, ...] = (), depth: int = 0) -> None:
    """Reject local/user/account identity aliases inside an inbound assignment."""
    if depth > 4 or not isinstance(value, Mapping):
        return
    for raw_key, raw_value in value.items():
        key = _safe_text(raw_key, "", 120).lower().replace("-", "_")
        current_path = path + (key,)
        parent = path[-1] if path else ""
        local_id = bool(_LOCAL_ID_KEY_RE.match(key))
        nested_generic_id = key == "id" and parent in {
            "user", "member", "membership", "app_user", "local_user", "account", "owner", "actor"
        }
        if (local_id or nested_generic_id) and key not in _CANONICAL_ID_KEYS:
            if raw_value not in (None, "", 0, False):
                raise ProjectAccessError(
                    "Local user, account or e-mail identity fields are not accepted by Chunk access projection.",
                    code=CODE_LOCAL_ID_REJECTED,
                    status_code=400,
                    details={"field": ".".join(current_path)},
                )
        if isinstance(raw_value, Mapping):
            _reject_local_identity_fields(raw_value, path=current_path, depth=depth + 1)


def _stable_json(value: Any) -> str:
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, default=str)
    except Exception:
        return "{}"


def normalize_chunk_project_id(value: Any) -> str:
    project_id = _safe_text(value, "", 255)
    if not project_id or not _PROJECT_ID_RE.fullmatch(project_id):
        raise ProjectAccessError(
            "A valid chunk project id is required.",
            code=CODE_INVALID_PROJECT,
            status_code=400,
        )
    return project_id


def normalize_auth_user_id(value: Any, *, strict: bool = True) -> str:
    if isinstance(value, bool) or value is None:
        raise ProjectAccessError(
            "A canonical auth_user_id is required.",
            code=CODE_CANONICAL_ID_REQUIRED,
            status_code=400,
        )
    if strict and isinstance(value, int):
        raise ProjectAccessError(
            "Local numeric user ids are not accepted as auth_user_id.",
            code=CODE_LOCAL_ID_REJECTED,
            status_code=400,
        )
    candidate = _safe_text(value, "", 255)
    if strict and candidate.isdigit():
        raise ProjectAccessError(
            "Local numeric user ids are not accepted as auth_user_id.",
            code=CODE_LOCAL_ID_REJECTED,
            status_code=400,
        )
    if not candidate or "@" in candidate or "/" in candidate or "\\" in candidate:
        raise ProjectAccessError(
            "A canonical opaque auth_user_id is required.",
            code=CODE_CANONICAL_ID_REQUIRED,
            status_code=400,
        )
    if not _CANONICAL_ID_RE.fullmatch(candidate):
        raise ProjectAccessError(
            "auth_user_id contains unsupported characters or length.",
            code=CODE_CANONICAL_ID_REQUIRED,
            status_code=400,
        )
    return candidate


def normalize_role(value: Any, *, allow_owner: bool = True, default: str = "") -> str:
    text = _safe_text(value, default, 40).lower().replace("-", "_").replace(" ", "_")
    role = _ROLE_ALIASES.get(text, text)
    if role not in ACCESS_ROLES or (role == ROLE_OWNER and not allow_owner):
        raise ProjectAccessError(
            "The project role is invalid for this operation.",
            code=CODE_ROLE_INVALID,
            status_code=400,
            details={"role": text or None, "allow_owner": allow_owner},
        )
    return role


def normalize_operation(value: Any) -> str:
    text = _safe_text(value, "", 120).lower().replace("-", "_").replace(" ", "_")
    operation = _OPERATION_ALIASES.get(text, text)
    if operation not in ALL_OPERATIONS or not _OPERATION_RE.fullmatch(operation):
        raise ProjectAccessError(
            "The project operation is unknown.",
            code=CODE_OPERATION_INVALID,
            status_code=400,
            details={"operation": text or None},
        )
    return operation


def role_capabilities(role: Any) -> tuple[str, ...]:
    try:
        normalized = normalize_role(role)
    except ProjectAccessError:
        return ()
    return DEFAULT_ROLE_CAPABILITIES.get(normalized, ())


def role_allows_operation(
    role: Any,
    operation: Any,
    *,
    viewer_allowed_operations: Optional[Iterable[str]] = None,
    viewer_denied_operations: Optional[Iterable[str]] = None,
) -> bool:
    try:
        normalized_role = normalize_role(role)
        normalized_operation = normalize_operation(operation)
    except ProjectAccessError:
        return False

    if normalized_role == ROLE_OWNER:
        return True
    if normalized_role == ROLE_ADMIN:
        return normalized_operation != OP_OWNER_TRANSFER
    if normalized_role == ROLE_EDITOR:
        return normalized_operation in READ_OPERATIONS | {
            OP_COMMANDS_EXECUTE,
            OP_CHUNKS_MATERIALIZE,
            OP_CHUNKS_WRITE,
            OP_WORLD_MUTATE,
        }

    allowed: set[str] = set()
    for item in viewer_allowed_operations or DEFAULT_VIEWER_ALLOWED_OPERATIONS:
        try:
            allowed.add(normalize_operation(item))
        except ProjectAccessError:
            continue
    denied: set[str] = set()
    for item in viewer_denied_operations or DEFAULT_VIEWER_DENIED_OPERATIONS:
        try:
            denied.add(normalize_operation(item))
        except ProjectAccessError:
            continue
    return normalized_operation in allowed and normalized_operation not in denied and normalized_operation in READ_OPERATIONS


# ---------------------------------------------------------------------------
# Errors and immutable contracts
# ---------------------------------------------------------------------------


class ProjectAccessError(RuntimeError):
    """Structured access-projection or authorization error."""

    def __init__(
        self,
        message: str,
        *,
        code: str = CODE_INTERNAL_ERROR,
        status_code: int = 500,
        retryable: bool = False,
        repair_required: bool = False,
        details: Optional[Mapping[str, Any]] = None,
        request_id: str = "",
        correlation_id: str = "",
    ) -> None:
        super().__init__(redact_access_text(message, max_len=2000) or "Project access error.")
        self.code = _safe_text(code, CODE_INTERNAL_ERROR, 120)
        self.status_code = _safe_int(status_code, 500, minimum=400, maximum=599)
        self.retryable = bool(retryable)
        self.repair_required = bool(repair_required)
        self.details = sanitize_access_mapping(details)
        self.request_id = _safe_text(request_id, "", 160)
        self.correlation_id = _safe_text(correlation_id, "", 160)

    def to_dict(self) -> dict[str, Any]:
        payload = {
            "ok": False,
            "code": self.code,
            "error": str(self),
            "status_code": self.status_code,
            "statusCode": self.status_code,
            "retryable": self.retryable,
            "repair_required": self.repair_required,
            "repairRequired": self.repair_required,
        }
        if self.request_id:
            payload["request_id"] = self.request_id
            payload["requestId"] = self.request_id
        if self.correlation_id:
            payload["correlation_id"] = self.correlation_id
            payload["correlationId"] = self.correlation_id
        if self.details:
            payload["details"] = dict(self.details)
        return payload


@dataclass(frozen=True, slots=True)
class AccessAssignment:
    """One normalized direct-user or group assignment."""

    auth_user_id: str
    role: str
    assignment_type: str = ASSIGNMENT_TYPE_DIRECT
    active: bool = True
    source_service: str = _DEFAULT_SOURCE_SERVICE
    managed: bool = True
    assignment_id: str = ""
    group_id: str = ""
    metadata: Mapping[str, Any] = field(default_factory=dict)
    updated_at: str = ""
    schema_version: str = PROJECT_ACCESS_SCHEMA_VERSION

    @property
    def is_direct(self) -> bool:
        return self.assignment_type == ASSIGNMENT_TYPE_DIRECT

    @property
    def is_group(self) -> bool:
        return self.assignment_type == ASSIGNMENT_TYPE_GROUP

    @property
    def is_owner(self) -> bool:
        return bool(self.active and self.role == ROLE_OWNER)

    @property
    def subject_fingerprint(self) -> str:
        return _short_fingerprint(self.auth_user_id or self.group_id, "sub")

    def to_dict(self, *, include_private: bool = False) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "schema_version": self.schema_version,
            "schemaVersion": self.schema_version,
            "role": self.role,
            "assignment_type": self.assignment_type,
            "assignmentType": self.assignment_type,
            "active": self.active,
            "managed": self.managed,
            "source_service": self.source_service or None,
            "sourceService": self.source_service or None,
            "subject_fingerprint": self.subject_fingerprint or None,
            "subjectFingerprint": self.subject_fingerprint or None,
            "owner": self.is_owner,
        }
        if self.updated_at:
            payload["updated_at"] = self.updated_at
            payload["updatedAt"] = self.updated_at
        if include_private:
            payload["auth_user_id"] = self.auth_user_id or None
            payload["authUserId"] = self.auth_user_id or None
            payload["group_id"] = self.group_id or None
            payload["groupId"] = self.group_id or None
            payload["assignment_id"] = self.assignment_id or None
            payload["assignmentId"] = self.assignment_id or None
            payload["metadata"] = sanitize_access_mapping(self.metadata)
        return payload


@dataclass(frozen=True, slots=True)
class AccessProjection:
    """Desired direct assignment projection supplied by vectoplan-app."""

    chunk_project_id: str
    assignments: tuple[AccessAssignment, ...]
    owner_auth_user_id: str
    source_service: str = _DEFAULT_SOURCE_SERVICE
    projection_version: str = _DEFAULT_PROJECTION_VERSION
    projection_fingerprint: str = ""
    request_id: str = ""
    correlation_id: str = ""
    idempotency_key_hash: str = ""
    metadata: Mapping[str, Any] = field(default_factory=dict)
    schema_version: str = PROJECT_ACCESS_PROJECTION_SCHEMA_VERSION

    @property
    def direct_assignments(self) -> tuple[AccessAssignment, ...]:
        return tuple(item for item in self.assignments if item.is_direct and item.active)

    @property
    def assignment_count(self) -> int:
        return len(self.direct_assignments)

    def role_map(self) -> dict[str, str]:
        return {item.auth_user_id: item.role for item in self.direct_assignments}

    def to_dict(self, *, include_private: bool = False) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "schema_version": self.schema_version,
            "schemaVersion": self.schema_version,
            "chunk_project_id": self.chunk_project_id,
            "chunkProjectId": self.chunk_project_id,
            "source_service": self.source_service,
            "sourceService": self.source_service,
            "projection_version": self.projection_version,
            "projectionVersion": self.projection_version,
            "projection_fingerprint": self.projection_fingerprint,
            "projectionFingerprint": self.projection_fingerprint,
            "owner_fingerprint": _short_fingerprint(self.owner_auth_user_id, "own"),
            "ownerFingerprint": _short_fingerprint(self.owner_auth_user_id, "own"),
            "assignment_count": self.assignment_count,
            "assignmentCount": self.assignment_count,
            "assignments": [item.to_dict(include_private=include_private) for item in self.direct_assignments],
            "request_id": self.request_id or None,
            "requestId": self.request_id or None,
            "correlation_id": self.correlation_id or None,
            "correlationId": self.correlation_id or None,
            "idempotency_key_hash": self.idempotency_key_hash or None,
            "idempotencyKeyHash": self.idempotency_key_hash or None,
        }
        if include_private:
            payload["owner_auth_user_id"] = self.owner_auth_user_id
            payload["ownerAuthUserId"] = self.owner_auth_user_id
            payload["metadata"] = sanitize_access_mapping(self.metadata)
        return payload


@dataclass(frozen=True, slots=True)
class AccessChange:
    """One reconciliation operation."""

    action: str
    auth_user_id: str
    before_role: str = ""
    after_role: str = ""
    assignment_type: str = ASSIGNMENT_TYPE_DIRECT
    reason: str = ""
    schema_version: str = PROJECT_ACCESS_PLAN_SCHEMA_VERSION

    @property
    def mutates(self) -> bool:
        return self.action in {CHANGE_ADD, CHANGE_UPDATE, CHANGE_REMOVE, CHANGE_TRANSFER_OWNER}

    def to_dict(self, *, include_private: bool = False) -> dict[str, Any]:
        payload = {
            "schema_version": self.schema_version,
            "schemaVersion": self.schema_version,
            "action": self.action,
            "subject_fingerprint": _short_fingerprint(self.auth_user_id, "sub"),
            "subjectFingerprint": _short_fingerprint(self.auth_user_id, "sub"),
            "before_role": self.before_role or None,
            "beforeRole": self.before_role or None,
            "after_role": self.after_role or None,
            "afterRole": self.after_role or None,
            "assignment_type": self.assignment_type,
            "assignmentType": self.assignment_type,
            "reason": self.reason or None,
            "mutates": self.mutates,
        }
        if include_private:
            payload["auth_user_id"] = self.auth_user_id
            payload["authUserId"] = self.auth_user_id
        return payload


@dataclass(frozen=True, slots=True)
class AccessSyncPlan:
    """Deterministic reconciliation plan."""

    projection: AccessProjection
    changes: tuple[AccessChange, ...]
    current_projection_fingerprint: str
    owner_transfer_required: bool = False
    current_owner_auth_user_id: str = ""
    desired_owner_auth_user_id: str = ""
    preserved_group_assignments: int = 0
    duplicate_direct_assignments: int = 0
    schema_version: str = PROJECT_ACCESS_PLAN_SCHEMA_VERSION

    @property
    def mutation_count(self) -> int:
        return sum(1 for item in self.changes if item.mutates)

    @property
    def no_changes(self) -> bool:
        return self.mutation_count == 0 and not self.owner_transfer_required

    def to_dict(self, *, include_private: bool = False) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "schema_version": self.schema_version,
            "schemaVersion": self.schema_version,
            "projection": self.projection.to_dict(include_private=include_private),
            "changes": [item.to_dict(include_private=include_private) for item in self.changes],
            "mutation_count": self.mutation_count,
            "mutationCount": self.mutation_count,
            "no_changes": self.no_changes,
            "noChanges": self.no_changes,
            "current_projection_fingerprint": self.current_projection_fingerprint,
            "currentProjectionFingerprint": self.current_projection_fingerprint,
            "owner_transfer_required": self.owner_transfer_required,
            "ownerTransferRequired": self.owner_transfer_required,
            "current_owner_fingerprint": _short_fingerprint(self.current_owner_auth_user_id, "own"),
            "currentOwnerFingerprint": _short_fingerprint(self.current_owner_auth_user_id, "own"),
            "desired_owner_fingerprint": _short_fingerprint(self.desired_owner_auth_user_id, "own"),
            "desiredOwnerFingerprint": _short_fingerprint(self.desired_owner_auth_user_id, "own"),
            "preserved_group_assignments": self.preserved_group_assignments,
            "preservedGroupAssignments": self.preserved_group_assignments,
            "duplicate_direct_assignments": self.duplicate_direct_assignments,
            "duplicateDirectAssignments": self.duplicate_direct_assignments,
        }
        if include_private:
            payload["current_owner_auth_user_id"] = self.current_owner_auth_user_id or None
            payload["currentOwnerAuthUserId"] = self.current_owner_auth_user_id or None
            payload["desired_owner_auth_user_id"] = self.desired_owner_auth_user_id or None
            payload["desiredOwnerAuthUserId"] = self.desired_owner_auth_user_id or None
        return payload


@dataclass(frozen=True, slots=True)
class AccessSyncResult:
    """Result of initialization or reconciliation."""

    ok: bool
    status: str
    code: str
    chunk_project_id: str
    status_code: int = 200
    projection_fingerprint: str = ""
    applied_changes: tuple[AccessChange, ...] = ()
    assignment_count: int = 0
    preserved_group_assignments: int = 0
    idempotent: bool = False
    verified: bool = False
    retryable: bool = False
    repair_required: bool = False
    error: str = ""
    request_id: str = ""
    correlation_id: str = ""
    elapsed_ms: float = 0.0
    details: Mapping[str, Any] = field(default_factory=dict)
    schema_version: str = PROJECT_ACCESS_RESULT_SCHEMA_VERSION

    def to_dict(self, *, include_private: bool = False) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "schema_version": self.schema_version,
            "schemaVersion": self.schema_version,
            "ok": self.ok,
            "status": self.status,
            "code": self.code,
            "status_code": self.status_code,
            "statusCode": self.status_code,
            "chunk_project_id": self.chunk_project_id or None,
            "chunkProjectId": self.chunk_project_id or None,
            "projection_fingerprint": self.projection_fingerprint or None,
            "projectionFingerprint": self.projection_fingerprint or None,
            "assignment_count": self.assignment_count,
            "assignmentCount": self.assignment_count,
            "preserved_group_assignments": self.preserved_group_assignments,
            "preservedGroupAssignments": self.preserved_group_assignments,
            "applied_change_count": len(self.applied_changes),
            "appliedChangeCount": len(self.applied_changes),
            "applied_changes": [item.to_dict(include_private=include_private) for item in self.applied_changes],
            "appliedChanges": [item.to_dict(include_private=include_private) for item in self.applied_changes],
            "idempotent": self.idempotent,
            "verified": self.verified,
            "retryable": self.retryable,
            "repair_required": self.repair_required,
            "repairRequired": self.repair_required,
            "request_id": self.request_id or None,
            "requestId": self.request_id or None,
            "correlation_id": self.correlation_id or None,
            "correlationId": self.correlation_id or None,
            "elapsed_ms": round(max(0.0, float(self.elapsed_ms)), 3),
            "elapsedMs": round(max(0.0, float(self.elapsed_ms)), 3),
        }
        if self.error:
            payload["error"] = redact_access_text(self.error, max_len=2000)
        if self.details:
            payload["details"] = sanitize_access_mapping(self.details)
        return payload

    def raise_for_error(self) -> "AccessSyncResult":
        if self.ok:
            return self
        raise ProjectAccessError(
            self.error or "Project access synchronization failed.",
            code=self.code,
            status_code=self.status_code,
            retryable=self.retryable,
            repair_required=self.repair_required,
            details=self.details,
            request_id=self.request_id,
            correlation_id=self.correlation_id,
        )


@dataclass(frozen=True, slots=True)
class AccessDecision:
    """Authorization decision for one project operation."""

    allowed: bool
    code: str
    chunk_project_id: str
    operation: str
    status_code: int = 200
    role: str = ""
    read_only: bool = True
    public: bool = False
    source: str = "direct"
    reason: str = ""
    auth_user_id: str = ""
    request_id: str = ""
    correlation_id: str = ""
    details: Mapping[str, Any] = field(default_factory=dict)
    schema_version: str = PROJECT_ACCESS_DECISION_SCHEMA_VERSION

    def to_dict(self, *, include_private: bool = False) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "schema_version": self.schema_version,
            "schemaVersion": self.schema_version,
            "allowed": self.allowed,
            "ok": self.allowed,
            "code": self.code,
            "status_code": self.status_code,
            "statusCode": self.status_code,
            "chunk_project_id": self.chunk_project_id,
            "chunkProjectId": self.chunk_project_id,
            "operation": self.operation,
            "role": self.role or None,
            "read_only": self.read_only,
            "readOnly": self.read_only,
            "public": self.public,
            "source": self.source,
            "reason": self.reason or None,
            "subject_fingerprint": _short_fingerprint(self.auth_user_id, "sub") or None,
            "subjectFingerprint": _short_fingerprint(self.auth_user_id, "sub") or None,
            "request_id": self.request_id or None,
            "requestId": self.request_id or None,
            "correlation_id": self.correlation_id or None,
            "correlationId": self.correlation_id or None,
        }
        if include_private:
            payload["auth_user_id"] = self.auth_user_id or None
            payload["authUserId"] = self.auth_user_id or None
        if self.details:
            payload["details"] = sanitize_access_mapping(self.details)
        return payload

    def require_allowed(self) -> "AccessDecision":
        if self.allowed:
            return self
        raise ProjectAccessError(
            self.reason or "Project access denied.",
            code=self.code or CODE_ACCESS_DENIED,
            status_code=self.status_code or 403,
            details=self.details,
            request_id=self.request_id,
            correlation_id=self.correlation_id,
        )


@dataclass(frozen=True, slots=True)
class OwnerTransferResult:
    """Result of the dedicated owner-transfer transaction."""

    ok: bool
    status: str
    code: str
    chunk_project_id: str
    status_code: int = 200
    old_owner_fingerprint: str = ""
    new_owner_fingerprint: str = ""
    former_owner_role: str = ""
    verified: bool = False
    retryable: bool = False
    repair_required: bool = False
    error: str = ""
    request_id: str = ""
    correlation_id: str = ""
    elapsed_ms: float = 0.0
    details: Mapping[str, Any] = field(default_factory=dict)
    schema_version: str = PROJECT_OWNER_TRANSFER_SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        payload = {
            "schema_version": self.schema_version,
            "schemaVersion": self.schema_version,
            "ok": self.ok,
            "status": self.status,
            "code": self.code,
            "status_code": self.status_code,
            "statusCode": self.status_code,
            "chunk_project_id": self.chunk_project_id,
            "chunkProjectId": self.chunk_project_id,
            "old_owner_fingerprint": self.old_owner_fingerprint or None,
            "oldOwnerFingerprint": self.old_owner_fingerprint or None,
            "new_owner_fingerprint": self.new_owner_fingerprint or None,
            "newOwnerFingerprint": self.new_owner_fingerprint or None,
            "former_owner_role": self.former_owner_role or None,
            "formerOwnerRole": self.former_owner_role or None,
            "verified": self.verified,
            "retryable": self.retryable,
            "repair_required": self.repair_required,
            "repairRequired": self.repair_required,
            "request_id": self.request_id or None,
            "requestId": self.request_id or None,
            "correlation_id": self.correlation_id or None,
            "correlationId": self.correlation_id or None,
            "elapsed_ms": round(max(0.0, float(self.elapsed_ms)), 3),
            "elapsedMs": round(max(0.0, float(self.elapsed_ms)), 3),
        }
        if self.error:
            payload["error"] = redact_access_text(self.error, max_len=2000)
        if self.details:
            payload["details"] = sanitize_access_mapping(self.details)
        return payload


# ---------------------------------------------------------------------------
# Configuration resolution
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _AccessSettings:
    enabled: bool
    default_deny: bool
    strict_canonical_user_ids: bool
    canonical_user_id_field: str
    allowed_roles: tuple[str, ...]
    viewer_read_only: bool
    allow_public_mutations: bool
    allow_identity_override: bool
    prune_stale_direct_assignments: bool
    preserve_group_assignments: bool
    verify_after_sync: bool
    source_service: str
    projection_version: str
    max_direct_assignments: int
    viewer_allowed_operations: tuple[str, ...]
    viewer_denied_operations: tuple[str, ...]
    runtime_business_mutations_enabled: bool
    service_auth_required: bool


def _config_value(name: str, default: Any, config: Any = None) -> Any:
    try:
        if isinstance(config, Mapping) and name in config:
            return config.get(name)
        if config is not None and hasattr(config, name):
            return getattr(config, name)
    except Exception:
        pass

    try:
        from flask import current_app

        if current_app and name in current_app.config:
            return current_app.config.get(name)
    except Exception:
        pass

    for module_name in ("config", "services.vectoplan_chunk.config", "vectoplan_chunk.config"):
        try:
            module = importlib.import_module(module_name)
            config_class = getattr(module, "BaseConfig", None)
            if config_class is not None and hasattr(config_class, name):
                return getattr(config_class, name)
        except Exception:
            continue
    return default


def _normalize_text_tuple(value: Any, default: Iterable[str]) -> tuple[str, ...]:
    if isinstance(value, str):
        items = re.split(r"[,;\s]+", value)
    else:
        items = _as_sequence(value)
    normalized: list[str] = []
    for item in items or list(default):
        text = _safe_text(item, "", 120).lower().replace("-", "_")
        if text and text not in normalized:
            normalized.append(text)
    return tuple(normalized)


def _load_access_settings(config: Any = None) -> _AccessSettings:
    roles = _normalize_text_tuple(
        _config_value("VECTOPLAN_CHUNK_ACCESS_ALLOWED_ROLES", ACCESS_ROLES, config),
        ACCESS_ROLES,
    )
    if tuple(roles) != ACCESS_ROLES:
        # Preserve the one-to-one role contract even if config is malformed.
        roles = ACCESS_ROLES

    viewer_allowed: list[str] = []
    for item in _normalize_text_tuple(
        _config_value(
            "VECTOPLAN_CHUNK_ACCESS_VIEWER_ALLOWED_OPERATIONS",
            DEFAULT_VIEWER_ALLOWED_OPERATIONS,
            config,
        ),
        DEFAULT_VIEWER_ALLOWED_OPERATIONS,
    ):
        try:
            normalized = normalize_operation(item)
        except ProjectAccessError:
            continue
        if normalized in READ_OPERATIONS and normalized not in viewer_allowed:
            viewer_allowed.append(normalized)

    viewer_denied: list[str] = []
    for item in _normalize_text_tuple(
        _config_value(
            "VECTOPLAN_CHUNK_ACCESS_VIEWER_DENIED_OPERATIONS",
            DEFAULT_VIEWER_DENIED_OPERATIONS,
            config,
        ),
        DEFAULT_VIEWER_DENIED_OPERATIONS,
    ):
        try:
            normalized = normalize_operation(item)
        except ProjectAccessError:
            continue
        if normalized not in viewer_denied:
            viewer_denied.append(normalized)

    return _AccessSettings(
        enabled=_safe_bool(_config_value("VECTOPLAN_CHUNK_ACCESS_CONTROL_ENABLED", True, config), True),
        default_deny=_safe_bool(_config_value("VECTOPLAN_CHUNK_ACCESS_DEFAULT_DENY", True, config), True),
        strict_canonical_user_ids=_safe_bool(
            _config_value("VECTOPLAN_CHUNK_ACCESS_STRICT_CANONICAL_USER_IDS", True, config),
            True,
        ),
        canonical_user_id_field=_safe_text(
            _config_value("VECTOPLAN_CHUNK_ACCESS_CANONICAL_USER_ID_FIELD", _DEFAULT_CANONICAL_ID_FIELD, config),
            _DEFAULT_CANONICAL_ID_FIELD,
            80,
        ),
        allowed_roles=tuple(roles),
        viewer_read_only=_safe_bool(
            _config_value("VECTOPLAN_CHUNK_ACCESS_VIEWER_READ_ONLY", True, config),
            True,
        ),
        allow_public_mutations=_safe_bool(
            _config_value("VECTOPLAN_CHUNK_ACCESS_ALLOW_PUBLIC_MUTATIONS", False, config),
            False,
        ),
        allow_identity_override=_safe_bool(
            _config_value("VECTOPLAN_CHUNK_ACCESS_ALLOW_IDENTITY_OVERRIDE", False, config),
            False,
        ),
        prune_stale_direct_assignments=_safe_bool(
            _config_value("VECTOPLAN_CHUNK_ACCESS_PRUNE_STALE_DIRECT_ASSIGNMENTS", True, config),
            True,
        ),
        preserve_group_assignments=_safe_bool(
            _config_value("VECTOPLAN_CHUNK_ACCESS_PRESERVE_GROUP_ASSIGNMENTS", True, config),
            True,
        ),
        verify_after_sync=_safe_bool(
            _config_value("VECTOPLAN_CHUNK_ACCESS_VERIFY_AFTER_SYNC", True, config),
            True,
        ),
        source_service=_safe_text(
            _config_value("VECTOPLAN_CHUNK_ACCESS_SOURCE_SERVICE", _DEFAULT_SOURCE_SERVICE, config),
            _DEFAULT_SOURCE_SERVICE,
            120,
        ),
        projection_version=_safe_text(
            _config_value("VECTOPLAN_CHUNK_ACCESS_PROJECTION_VERSION", _DEFAULT_PROJECTION_VERSION, config),
            _DEFAULT_PROJECTION_VERSION,
            120,
        ),
        max_direct_assignments=_safe_int(
            _config_value("VECTOPLAN_CHUNK_ACCESS_MAX_DIRECT_ASSIGNMENTS", 10000, config),
            10000,
            minimum=1,
            maximum=1_000_000,
        ),
        viewer_allowed_operations=tuple(viewer_allowed or DEFAULT_VIEWER_ALLOWED_OPERATIONS),
        viewer_denied_operations=tuple(viewer_denied or DEFAULT_VIEWER_DENIED_OPERATIONS),
        runtime_business_mutations_enabled=_safe_bool(
            _config_value("VECTOPLAN_CHUNK_RUNTIME_BUSINESS_MUTATIONS_ENABLED", True, config),
            True,
        ),
        service_auth_required=_safe_bool(
            _config_value("VECTOPLAN_CHUNK_SERVICE_AUTH_REQUIRED", True, config),
            True,
        ),
    )


# ---------------------------------------------------------------------------
# Service principal helpers
# ---------------------------------------------------------------------------


def _import_service_auth_module() -> Any:
    for module_name in (
        ".service_auth_service",
        "service_auth_service",
        "src.services.service_auth_service",
    ):
        try:
            if module_name.startswith("."):
                return importlib.import_module(module_name, package=__package__)
            return importlib.import_module(module_name)
        except Exception:
            continue
    return None


def _resolve_service_principal(principal: Any = None) -> Any:
    if principal is not None:
        return principal
    module = _import_service_auth_module()
    if module is not None:
        try:
            return module.get_current_service_principal(None)
        except Exception:
            return None
    return None


def _principal_service_id(principal: Any) -> str:
    return _safe_text(_get_value(principal, "service_id", "serviceId", default=""), "", 120)


def _principal_authenticated(principal: Any) -> bool:
    trusted = _get_value(principal, "is_trusted_service", default=None)
    if trusted is not None:
        return _safe_bool(trusted, False)
    return _safe_bool(_get_value(principal, "authenticated", default=False), False) and not _safe_bool(
        _get_value(principal, "exempt", default=False),
        False,
    )


def _principal_request_id(principal: Any) -> str:
    return _safe_text(_get_value(principal, "request_id", "requestId", default=""), "", 160)


def _principal_correlation_id(principal: Any) -> str:
    return _safe_text(_get_value(principal, "correlation_id", "correlationId", default=""), "", 160)


def _principal_idempotency_hash(principal: Any) -> str:
    return _safe_text(
        _get_value(principal, "idempotency_key_hash", "idempotencyKeyHash", default=""),
        "",
        128,
    )


def require_project_access_service_principal(
    *,
    principal: Any = None,
    allowed_service_ids: Optional[Iterable[str]] = None,
    config: Any = None,
) -> Any:
    settings = _load_access_settings(config)
    resolved = _resolve_service_principal(principal)
    if not settings.service_auth_required:
        return resolved
    if resolved is None or not _principal_authenticated(resolved):
        raise ProjectAccessError(
            "An authenticated internal service is required for project access mutation.",
            code=CODE_SERVICE_AUTH_REQUIRED,
            status_code=401,
        )
    allowed = {
        _safe_text(item, "", 120)
        for item in (allowed_service_ids or _DEFAULT_MUTATION_SERVICES)
        if _safe_text(item, "", 120)
    }
    service_id = _principal_service_id(resolved)
    if not service_id or service_id not in allowed:
        raise ProjectAccessError(
            "The authenticated service is not allowed for this project access operation.",
            code=CODE_SERVICE_NOT_ALLOWED,
            status_code=403,
            details={"service_id": service_id or None, "allowed_service_ids": sorted(allowed)},
            request_id=_principal_request_id(resolved),
            correlation_id=_principal_correlation_id(resolved),
        )
    return resolved


# ---------------------------------------------------------------------------
# Projection parsing and fingerprinting
# ---------------------------------------------------------------------------


def _extract_canonical_id_from_assignment(
    item: Any,
    *,
    settings: _AccessSettings,
) -> str:
    data = _as_mapping(item)
    if data:
        _reject_local_identity_fields(data)
    if not data and item is not None:
        data = {
            settings.canonical_user_id_field: _get_value(
                item,
                settings.canonical_user_id_field,
                "auth_user_id",
                "authUserId",
                default=None,
            )
        }
    for key in data:
        normalized = _safe_text(key, "", 120).lower().replace("-", "_")
        if _LOCAL_ID_KEY_RE.match(normalized) and normalized not in _CANONICAL_ID_KEYS:
            if data.get(key) not in (None, "", 0):
                raise ProjectAccessError(
                    "Local user, account or e-mail identity fields are not accepted by Chunk access projection.",
                    code=CODE_LOCAL_ID_REJECTED,
                    status_code=400,
                    details={"field": normalized},
                )
    value = _get_value(
        data,
        settings.canonical_user_id_field,
        "auth_user_id",
        "authUserId",
        "subject_auth_user_id",
        "subjectAuthUserId",
        default=None,
    )
    return normalize_auth_user_id(value, strict=settings.strict_canonical_user_ids)


def normalize_access_assignment(
    item: Any,
    *,
    settings: Optional[_AccessSettings] = None,
    allow_group: bool = False,
    default_source_service: str = _DEFAULT_SOURCE_SERVICE,
) -> AccessAssignment:
    resolved_settings = settings or _load_access_settings()
    data = _as_mapping(item)
    assignment_type = _safe_text(
        _get_value(data or item, "assignment_type", "assignmentType", "subject_type", "subjectType", default=ASSIGNMENT_TYPE_DIRECT),
        ASSIGNMENT_TYPE_DIRECT,
        40,
    ).lower().replace("-", "_")
    if assignment_type in {"user", "auth_user", "principal"}:
        assignment_type = ASSIGNMENT_TYPE_DIRECT
    if assignment_type in {"team", "role_group"}:
        assignment_type = ASSIGNMENT_TYPE_GROUP
    if assignment_type not in ASSIGNMENT_TYPES:
        raise ProjectAccessError(
            "The assignment type is invalid.",
            code=CODE_INVALID_PROJECTION,
            status_code=400,
            details={"assignment_type": assignment_type},
        )
    if assignment_type == ASSIGNMENT_TYPE_GROUP:
        if not allow_group:
            raise ProjectAccessError(
                "App project access projection may contain direct auth users only.",
                code=CODE_INVALID_PROJECTION,
                status_code=400,
            )
        group_id = _safe_text(
            _get_value(data or item, "group_id", "groupId", "subject_id", "subjectId", default=""),
            "",
            255,
        )
        if not group_id:
            raise ProjectAccessError(
                "A group assignment requires group_id.",
                code=CODE_INVALID_PROJECTION,
                status_code=400,
            )
        auth_user_id = ""
    else:
        group_id = ""
        auth_user_id = _extract_canonical_id_from_assignment(item, settings=resolved_settings)

    role = normalize_role(_get_value(data or item, "role", "project_role", "projectRole", default=""))
    source_service = _safe_text(
        _get_value(data or item, "source_service", "sourceService", "managed_by", "managedBy", default=default_source_service),
        default_source_service,
        120,
    )
    return AccessAssignment(
        auth_user_id=auth_user_id,
        role=role,
        assignment_type=assignment_type,
        active=_safe_bool(_get_value(data or item, "active", "enabled", default=True), True),
        source_service=source_service,
        managed=_safe_bool(_get_value(data or item, "managed", "direct_managed", "directManaged", default=True), True),
        assignment_id=_safe_text(_get_value(data or item, "assignment_id", "assignmentId", "public_id", "publicId", default=""), "", 255),
        group_id=group_id,
        metadata=sanitize_access_mapping(_get_value(data or item, "metadata", "metadata_json", "metadataJson", default={})),
        updated_at=_safe_text(_get_value(data or item, "updated_at", "updatedAt", default=""), "", 100),
    )


def _iter_desired_assignment_items(payload: Any) -> list[Any]:
    data = _as_mapping(payload)
    for key in (
        "assignments",
        "direct_assignments",
        "directAssignments",
        "members",
        "access_assignments",
        "accessAssignments",
    ):
        value = data.get(key)
        if isinstance(value, Mapping):
            return [
                {"auth_user_id": auth_user_id, "role": role}
                for auth_user_id, role in value.items()
            ]
        sequence = _as_sequence(value)
        if sequence:
            return sequence
    roles = data.get("roles")
    if isinstance(roles, Mapping):
        return [{"auth_user_id": key, "role": value} for key, value in roles.items()]
    if isinstance(payload, Mapping):
        # A bare canonical-id to role mapping is accepted only when every value is scalar.
        if payload and all(not isinstance(value, (Mapping, list, tuple, set)) for value in payload.values()):
            reserved = {
                "owner_auth_user_id",
                "ownerAuthUserId",
                "chunk_project_id",
                "chunkProjectId",
                "source_service",
                "projection_version",
                "metadata",
            }
            if not any(key in reserved for key in payload):
                return [{"auth_user_id": key, "role": value} for key, value in payload.items()]
    return []


def build_projection_fingerprint(
    chunk_project_id: Any,
    assignments: Iterable[AccessAssignment],
    *,
    projection_version: str = _DEFAULT_PROJECTION_VERSION,
    source_service: str = _DEFAULT_SOURCE_SERVICE,
) -> str:
    project_id = normalize_chunk_project_id(chunk_project_id)
    canonical = {
        "chunk_project_id": project_id,
        "projection_version": _safe_text(projection_version, _DEFAULT_PROJECTION_VERSION, 120),
        "source_service": _safe_text(source_service, _DEFAULT_SOURCE_SERVICE, 120),
        "assignments": [
            {
                "auth_user_id": item.auth_user_id,
                "role": item.role,
                "assignment_type": item.assignment_type,
                "active": bool(item.active),
            }
            for item in sorted(
                [entry for entry in assignments if entry.is_direct and entry.active],
                key=lambda entry: (entry.auth_user_id, entry.role),
            )
        ],
    }
    return hashlib.sha256(_stable_json(canonical).encode("utf-8")).hexdigest()


def build_access_projection(
    chunk_project_id: Any,
    payload: Any,
    *,
    principal: Any = None,
    request_id: str = "",
    correlation_id: str = "",
    idempotency_key: str = "",
    config: Any = None,
) -> AccessProjection:
    settings = _load_access_settings(config)
    project_id = normalize_chunk_project_id(chunk_project_id)
    data = _as_mapping(payload)

    items = _iter_desired_assignment_items(payload)
    normalized_by_id: dict[str, AccessAssignment] = {}
    for raw_item in items:
        assignment = normalize_access_assignment(
            raw_item,
            settings=settings,
            allow_group=False,
            default_source_service=settings.source_service,
        )
        if not assignment.active:
            continue
        existing = normalized_by_id.get(assignment.auth_user_id)
        if existing is not None and existing.role != assignment.role:
            raise ProjectAccessError(
                "The same auth_user_id appears with conflicting roles.",
                code=CODE_DUPLICATE_ASSIGNMENT,
                status_code=400,
                repair_required=True,
                details={"subject_fingerprint": assignment.subject_fingerprint},
            )
        normalized_by_id[assignment.auth_user_id] = assignment

    owner_value = _get_value(
        data,
        "owner_auth_user_id",
        "ownerAuthUserId",
        "auth_owner_user_id",
        "authOwnerUserId",
        default=None,
    )
    if owner_value not in (None, ""):
        owner_id = normalize_auth_user_id(owner_value, strict=settings.strict_canonical_user_ids)
        existing_owner = normalized_by_id.get(owner_id)
        if existing_owner is not None and existing_owner.role != ROLE_OWNER:
            raise ProjectAccessError(
                "The declared owner has a conflicting non-owner role.",
                code=CODE_MULTIPLE_OWNERS,
                status_code=400,
                repair_required=True,
            )
        normalized_by_id[owner_id] = AccessAssignment(
            auth_user_id=owner_id,
            role=ROLE_OWNER,
            source_service=settings.source_service,
        )

    assignments = tuple(sorted(normalized_by_id.values(), key=lambda item: (ROLE_RANK[item.role] * -1, item.auth_user_id)))
    if len(assignments) > settings.max_direct_assignments:
        raise ProjectAccessError(
            "The direct assignment projection exceeds the configured limit.",
            code=CODE_ASSIGNMENT_LIMIT,
            status_code=413,
            details={"assignment_count": len(assignments), "maximum": settings.max_direct_assignments},
        )

    owners = [item for item in assignments if item.role == ROLE_OWNER]
    if not owners:
        raise ProjectAccessError(
            "The desired access projection requires exactly one owner.",
            code=CODE_OWNER_REQUIRED,
            status_code=400,
            repair_required=True,
        )
    if len(owners) != 1:
        raise ProjectAccessError(
            "The desired access projection contains multiple owners.",
            code=CODE_MULTIPLE_OWNERS,
            status_code=400,
            repair_required=True,
            details={"owner_count": len(owners)},
        )

    source_service = _safe_text(
        _get_value(data, "source_service", "sourceService", default=settings.source_service),
        settings.source_service,
        120,
    )
    if source_service != settings.source_service:
        raise ProjectAccessError(
            "The access projection source service is not accepted.",
            code=CODE_IDENTITY_OVERRIDE_DENIED,
            status_code=403,
            details={"source_service": source_service},
        )
    projection_version = _safe_text(
        _get_value(data, "projection_version", "projectionVersion", default=settings.projection_version),
        settings.projection_version,
        120,
    )

    resolved_principal = _resolve_service_principal(principal)
    resolved_request_id = _safe_text(request_id or _principal_request_id(resolved_principal), "", 160)
    resolved_correlation_id = _safe_text(
        correlation_id or _principal_correlation_id(resolved_principal) or resolved_request_id,
        "",
        160,
    )
    idempotency_hash = _hash_text(idempotency_key) or _principal_idempotency_hash(resolved_principal)
    fingerprint = build_projection_fingerprint(
        project_id,
        assignments,
        projection_version=projection_version,
        source_service=source_service,
    )
    return AccessProjection(
        chunk_project_id=project_id,
        assignments=assignments,
        owner_auth_user_id=owners[0].auth_user_id,
        source_service=source_service,
        projection_version=projection_version,
        projection_fingerprint=fingerprint,
        request_id=resolved_request_id,
        correlation_id=resolved_correlation_id,
        idempotency_key_hash=idempotency_hash,
        metadata=sanitize_access_mapping(_get_value(data, "metadata", "meta", default={})),
    )


# ---------------------------------------------------------------------------
# Repository abstractions
# ---------------------------------------------------------------------------


class ProjectAccessRepository:
    """Minimal repository contract used by this service."""

    def list_assignments(self, chunk_project_id: str) -> list[Any]:  # pragma: no cover - interface
        raise NotImplementedError

    def upsert_direct_assignment(self, chunk_project_id: str, assignment: AccessAssignment) -> Any:  # pragma: no cover
        raise NotImplementedError

    def delete_direct_assignment(self, chunk_project_id: str, auth_user_id: str) -> bool:  # pragma: no cover
        raise NotImplementedError

    def get_project_access_state(self, chunk_project_id: str) -> Mapping[str, Any]:
        return {}

    def set_project_access_state(self, chunk_project_id: str, state: Mapping[str, Any]) -> None:
        return None

    def flush(self) -> None:
        return None

    def commit(self) -> None:
        return None

    def rollback(self) -> None:
        return None

    def transaction(self, *, commit: bool = True) -> ContextManager[Any]:
        return nullcontext()


class InMemoryProjectAccessRepository(ProjectAccessRepository):
    """Thread-safe repository suitable for tests and local adapters."""

    def __init__(self) -> None:
        self._assignments: dict[str, list[AccessAssignment]] = {}
        self._states: dict[str, dict[str, Any]] = {}
        self._lock = threading.RLock()

    def list_assignments(self, chunk_project_id: str) -> list[AccessAssignment]:
        project_id = normalize_chunk_project_id(chunk_project_id)
        with self._lock:
            return copy.deepcopy(self._assignments.get(project_id, []))

    def upsert_direct_assignment(self, chunk_project_id: str, assignment: AccessAssignment) -> AccessAssignment:
        project_id = normalize_chunk_project_id(chunk_project_id)
        if not assignment.is_direct:
            raise ProjectAccessError(
                "Only direct assignments may be upserted through this method.",
                code=CODE_REPOSITORY_ERROR,
                status_code=500,
            )
        with self._lock:
            entries = self._assignments.setdefault(project_id, [])
            replacement = replace(assignment, updated_at=_utcnow_iso())
            for index, existing in enumerate(entries):
                if existing.is_direct and _constant_equal(existing.auth_user_id, assignment.auth_user_id):
                    entries[index] = replacement
                    return copy.deepcopy(replacement)
            entries.append(replacement)
            return copy.deepcopy(replacement)

    def add_group_assignment(self, chunk_project_id: str, group_id: str, role: str) -> AccessAssignment:
        project_id = normalize_chunk_project_id(chunk_project_id)
        entry = AccessAssignment(
            auth_user_id="",
            group_id=_safe_text(group_id, "", 255),
            role=normalize_role(role),
            assignment_type=ASSIGNMENT_TYPE_GROUP,
            managed=False,
            source_service="group-directory",
            updated_at=_utcnow_iso(),
        )
        with self._lock:
            self._assignments.setdefault(project_id, []).append(entry)
        return copy.deepcopy(entry)

    def delete_direct_assignment(self, chunk_project_id: str, auth_user_id: str) -> bool:
        project_id = normalize_chunk_project_id(chunk_project_id)
        canonical_id = normalize_auth_user_id(auth_user_id)
        with self._lock:
            entries = self._assignments.setdefault(project_id, [])
            retained = [
                item
                for item in entries
                if not (item.is_direct and _constant_equal(item.auth_user_id, canonical_id))
            ]
            changed = len(retained) != len(entries)
            self._assignments[project_id] = retained
            return changed

    def get_project_access_state(self, chunk_project_id: str) -> Mapping[str, Any]:
        project_id = normalize_chunk_project_id(chunk_project_id)
        with self._lock:
            return copy.deepcopy(self._states.get(project_id, {}))

    def set_project_access_state(self, chunk_project_id: str, state: Mapping[str, Any]) -> None:
        project_id = normalize_chunk_project_id(chunk_project_id)
        with self._lock:
            self._states[project_id] = copy.deepcopy(dict(state))

    @contextmanager
    def transaction(self, *, commit: bool = True) -> Iterator["InMemoryProjectAccessRepository"]:
        del commit
        with self._lock:
            snapshot_assignments = copy.deepcopy(self._assignments)
            snapshot_states = copy.deepcopy(self._states)
            try:
                yield self
            except Exception:
                self._assignments = snapshot_assignments
                self._states = snapshot_states
                raise


class SQLAlchemyProjectAccessRepository(ProjectAccessRepository):
    """Adaptive SQLAlchemy-style repository with explicit model injection.

    The adapter deliberately avoids importing SQLAlchemy.  It uses the familiar
    ``Model.query`` or ``session.query(Model)`` interfaces and configurable field
    candidates.  A dedicated repository remains preferable for production when
    model names differ materially.
    """

    DEFAULT_FIELDS: dict[str, tuple[str, ...]] = {
        "project": ("chunk_project_id", "project_public_id", "project_id"),
        "auth_user": ("auth_user_id", "subject_auth_user_id", "principal_id", "subject_id"),
        "role": ("role", "project_role", "access_role"),
        "assignment_type": ("assignment_type", "subject_type", "principal_type"),
        "active": ("active", "is_active", "enabled"),
        "managed": ("managed", "is_managed", "direct_managed"),
        "source_service": ("source_service", "managed_by", "source"),
        "group": ("group_id", "subject_group_id"),
        "metadata": ("metadata_json", "details", "payload"),
        "assignment_id": ("public_id", "assignment_id", "id"),
        "updated_at": ("updated_at",),
        "state_fingerprint": ("access_projection_fingerprint", "projection_fingerprint"),
        "state_status": ("access_sync_status", "projection_status"),
        "state_version": ("access_projection_version", "projection_version"),
        "state_updated_at": ("access_sync_updated_at", "projection_updated_at"),
    }

    def __init__(
        self,
        *,
        session: Any,
        assignment_model: Any,
        project_model: Any = None,
        field_map: Optional[Mapping[str, Any]] = None,
    ) -> None:
        if session is None or assignment_model is None:
            raise ProjectAccessError(
                "session and assignment_model are required for SQLAlchemyProjectAccessRepository.",
                code=CODE_REPOSITORY_REQUIRED,
                status_code=500,
            )
        self.session = session
        self.assignment_model = assignment_model
        self.project_model = project_model
        self.field_map: dict[str, tuple[str, ...]] = dict(self.DEFAULT_FIELDS)
        for key, value in dict(field_map or {}).items():
            candidates = (value,) if isinstance(value, str) else tuple(_as_sequence(value))
            normalized = tuple(_safe_text(item, "", 120) for item in candidates if _safe_text(item, "", 120))
            if normalized:
                self.field_map[key] = normalized

    def _field_name(self, model_or_record: Any, key: str, *, required: bool = False) -> str:
        for name in self.field_map.get(key, ()):
            try:
                if hasattr(model_or_record, name):
                    return name
            except Exception:
                continue
        if required:
            raise ProjectAccessError(
                f"The persistence model is missing the required {key} field.",
                code=CODE_REPOSITORY_ERROR,
                status_code=500,
                details={"field_role": key},
            )
        return ""

    def _query(self, model: Any) -> Any:
        query = getattr(model, "query", None)
        if query is not None:
            return query
        method = getattr(self.session, "query", None)
        if callable(method):
            return method(model)
        raise ProjectAccessError(
            "The persistence session does not provide a query interface.",
            code=CODE_REPOSITORY_ERROR,
            status_code=500,
        )

    def _project_filter(self, query: Any, model: Any, chunk_project_id: str) -> Any:
        field_name = self._field_name(model, "project", required=True)
        column = getattr(model, field_name)
        return query.filter(column == chunk_project_id)

    def _records(self, chunk_project_id: str) -> list[Any]:
        query = self._project_filter(self._query(self.assignment_model), self.assignment_model, chunk_project_id)
        return list(query.all())

    def _record_to_assignment(self, record: Any) -> AccessAssignment:
        assignment_type_field = self._field_name(record, "assignment_type")
        assignment_type = _safe_text(
            getattr(record, assignment_type_field, ASSIGNMENT_TYPE_DIRECT) if assignment_type_field else ASSIGNMENT_TYPE_DIRECT,
            ASSIGNMENT_TYPE_DIRECT,
            40,
        ).lower()
        if assignment_type in {"user", "principal", "auth_user"}:
            assignment_type = ASSIGNMENT_TYPE_DIRECT
        if assignment_type in {"team", "role_group"}:
            assignment_type = ASSIGNMENT_TYPE_GROUP

        role_field = self._field_name(record, "role", required=True)
        auth_field = self._field_name(record, "auth_user")
        group_field = self._field_name(record, "group")
        metadata_field = self._field_name(record, "metadata")
        active_field = self._field_name(record, "active")
        managed_field = self._field_name(record, "managed")
        source_field = self._field_name(record, "source_service")
        id_field = self._field_name(record, "assignment_id")
        updated_field = self._field_name(record, "updated_at")

        auth_user_id = ""
        group_id = ""
        if assignment_type == ASSIGNMENT_TYPE_GROUP:
            group_id = _safe_text(getattr(record, group_field, "") if group_field else "", "", 255)
        else:
            auth_user_id = normalize_auth_user_id(
                getattr(record, auth_field, None) if auth_field else None,
                strict=False,
            )

        return AccessAssignment(
            auth_user_id=auth_user_id,
            group_id=group_id,
            role=normalize_role(getattr(record, role_field, "")),
            assignment_type=assignment_type if assignment_type in ASSIGNMENT_TYPES else ASSIGNMENT_TYPE_DIRECT,
            active=_safe_bool(getattr(record, active_field, True) if active_field else True, True),
            managed=_safe_bool(getattr(record, managed_field, assignment_type == ASSIGNMENT_TYPE_DIRECT) if managed_field else assignment_type == ASSIGNMENT_TYPE_DIRECT, assignment_type == ASSIGNMENT_TYPE_DIRECT),
            source_service=_safe_text(getattr(record, source_field, _DEFAULT_SOURCE_SERVICE) if source_field else _DEFAULT_SOURCE_SERVICE, _DEFAULT_SOURCE_SERVICE, 120),
            assignment_id=_safe_text(getattr(record, id_field, "") if id_field else "", "", 255),
            metadata=sanitize_access_mapping(getattr(record, metadata_field, {}) if metadata_field else {}),
            updated_at=_safe_text(getattr(record, updated_field, "") if updated_field else "", "", 100),
        )

    def list_assignments(self, chunk_project_id: str) -> list[AccessAssignment]:
        return [self._record_to_assignment(record) for record in self._records(chunk_project_id)]

    def _find_direct_record(self, chunk_project_id: str, auth_user_id: str) -> Any:
        project_field = self._field_name(self.assignment_model, "project", required=True)
        auth_field = self._field_name(self.assignment_model, "auth_user", required=True)
        query = self._query(self.assignment_model).filter(
            getattr(self.assignment_model, project_field) == chunk_project_id,
            getattr(self.assignment_model, auth_field) == auth_user_id,
        )
        assignment_type_field = self._field_name(self.assignment_model, "assignment_type")
        if assignment_type_field:
            column = getattr(self.assignment_model, assignment_type_field)
            try:
                query = query.filter(column.in_((ASSIGNMENT_TYPE_DIRECT, "user", "principal", "auth_user")))
            except Exception:
                query = query.filter(column == ASSIGNMENT_TYPE_DIRECT)
        if hasattr(query, "one_or_none"):
            return query.one_or_none()
        results = list(query.limit(2).all())
        if len(results) > 1:
            raise ProjectAccessError(
                "Duplicate direct assignments exist for one auth_user_id.",
                code=CODE_DUPLICATE_ASSIGNMENT,
                status_code=409,
                repair_required=True,
            )
        return results[0] if results else None

    def upsert_direct_assignment(self, chunk_project_id: str, assignment: AccessAssignment) -> Any:
        record = self._find_direct_record(chunk_project_id, assignment.auth_user_id)
        if record is None:
            try:
                record = self.assignment_model()
            except Exception as exc:
                raise ProjectAccessError(
                    "The assignment model could not be constructed.",
                    code=CODE_REPOSITORY_ERROR,
                    status_code=500,
                    details={"error_type": type(exc).__name__},
                ) from exc
            self.session.add(record)
        project_field = self._field_name(record, "project", required=True)
        auth_field = self._field_name(record, "auth_user", required=True)
        role_field = self._field_name(record, "role", required=True)
        _set_value(record, project_field, chunk_project_id)
        _set_value(record, auth_field, assignment.auth_user_id)
        _set_value(record, role_field, assignment.role)
        optional_values = {
            "assignment_type": ASSIGNMENT_TYPE_DIRECT,
            "active": True,
            "managed": True,
            "source_service": assignment.source_service,
            "metadata": dict(assignment.metadata),
        }
        for key, value in optional_values.items():
            field_name = self._field_name(record, key)
            if field_name:
                _set_value(record, field_name, value)
        return record

    def delete_direct_assignment(self, chunk_project_id: str, auth_user_id: str) -> bool:
        record = self._find_direct_record(chunk_project_id, auth_user_id)
        if record is None:
            return False
        self.session.delete(record)
        return True

    def _project_record(self, chunk_project_id: str) -> Any:
        if self.project_model is None:
            return None
        query = self._project_filter(self._query(self.project_model), self.project_model, chunk_project_id)
        if hasattr(query, "one_or_none"):
            return query.one_or_none()
        results = list(query.limit(2).all())
        return results[0] if results else None

    def get_project_access_state(self, chunk_project_id: str) -> Mapping[str, Any]:
        record = self._project_record(chunk_project_id)
        if record is None:
            return {}
        state: dict[str, Any] = {}
        for key in ("state_fingerprint", "state_status", "state_version", "state_updated_at"):
            field_name = self._field_name(record, key)
            if field_name:
                state[key] = getattr(record, field_name, None)
        return state

    def set_project_access_state(self, chunk_project_id: str, state: Mapping[str, Any]) -> None:
        record = self._project_record(chunk_project_id)
        if record is None:
            return
        mapping = {
            "state_fingerprint": state.get("projection_fingerprint"),
            "state_status": state.get("status"),
            "state_version": state.get("projection_version"),
            "state_updated_at": state.get("updated_at"),
        }
        for key, value in mapping.items():
            field_name = self._field_name(record, key)
            if field_name:
                _set_value(record, field_name, value)

    def flush(self) -> None:
        method = getattr(self.session, "flush", None)
        if callable(method):
            method()

    def commit(self) -> None:
        method = getattr(self.session, "commit", None)
        if callable(method):
            method()

    def rollback(self) -> None:
        method = getattr(self.session, "rollback", None)
        if callable(method):
            method()

    @contextmanager
    def transaction(self, *, commit: bool = True) -> Iterator["SQLAlchemyProjectAccessRepository"]:
        nested = getattr(self.session, "begin_nested", None)
        context: Any = nested() if callable(nested) else nullcontext()
        try:
            with context:
                yield self
                self.flush()
            if commit:
                self.commit()
        except Exception:
            if commit:
                self.rollback()
            raise


def resolve_project_access_repository(
    repository: Any = None,
    *,
    session: Any = None,
    assignment_model: Any = None,
    project_model: Any = None,
    field_map: Optional[Mapping[str, Any]] = None,
) -> ProjectAccessRepository:
    if isinstance(repository, ProjectAccessRepository):
        return repository
    if repository is not None:
        required = ("list_assignments", "upsert_direct_assignment", "delete_direct_assignment")
        if all(callable(getattr(repository, name, None)) for name in required):
            return repository
    if session is not None and assignment_model is not None:
        return SQLAlchemyProjectAccessRepository(
            session=session,
            assignment_model=assignment_model,
            project_model=project_model,
            field_map=field_map,
        )
    raise ProjectAccessError(
        "A project access repository is required.",
        code=CODE_REPOSITORY_REQUIRED,
        status_code=500,
    )


# ---------------------------------------------------------------------------
# Reconciliation planning
# ---------------------------------------------------------------------------


def _normalize_current_assignments(
    records: Iterable[Any],
    *,
    settings: _AccessSettings,
) -> tuple[list[AccessAssignment], list[AccessAssignment]]:
    direct: list[AccessAssignment] = []
    groups: list[AccessAssignment] = []
    for record in list(records or []):
        assignment = record if isinstance(record, AccessAssignment) else normalize_access_assignment(
            record,
            settings=settings,
            allow_group=True,
            default_source_service=settings.source_service,
        )
        if not assignment.active:
            continue
        if assignment.is_group:
            groups.append(assignment)
        else:
            direct.append(assignment)
    return direct, groups


def _direct_role_map(assignments: Iterable[AccessAssignment]) -> tuple[dict[str, str], int]:
    roles: dict[str, str] = {}
    duplicates = 0
    for item in assignments:
        existing = roles.get(item.auth_user_id)
        if existing is not None:
            duplicates += 1
            if existing != item.role:
                raise ProjectAccessError(
                    "Conflicting duplicate direct assignments require repair.",
                    code=CODE_DUPLICATE_ASSIGNMENT,
                    status_code=409,
                    repair_required=True,
                    details={"subject_fingerprint": item.subject_fingerprint},
                )
        roles[item.auth_user_id] = item.role
    return roles, duplicates


def _owner_from_role_map(role_map: Mapping[str, str]) -> str:
    owners = [auth_user_id for auth_user_id, role in role_map.items() if role == ROLE_OWNER]
    if len(owners) > 1:
        raise ProjectAccessError(
            "The persisted projection contains multiple owners.",
            code=CODE_MULTIPLE_OWNERS,
            status_code=409,
            repair_required=True,
            details={"owner_count": len(owners)},
        )
    return owners[0] if owners else ""


def build_current_projection_fingerprint(
    chunk_project_id: Any,
    assignments: Iterable[AccessAssignment],
    *,
    projection_version: str = _DEFAULT_PROJECTION_VERSION,
    source_service: str = _DEFAULT_SOURCE_SERVICE,
) -> str:
    return build_projection_fingerprint(
        chunk_project_id,
        assignments,
        projection_version=projection_version,
        source_service=source_service,
    )


def plan_project_access_reconciliation(
    projection: AccessProjection,
    current_assignments: Iterable[Any],
    *,
    config: Any = None,
) -> AccessSyncPlan:
    settings = _load_access_settings(config)
    direct, groups = _normalize_current_assignments(current_assignments, settings=settings)
    current_roles, duplicate_count = _direct_role_map(direct)
    desired_roles = projection.role_map()
    current_owner = _owner_from_role_map(current_roles)
    desired_owner = projection.owner_auth_user_id
    owner_transfer_required = bool(current_owner and not _constant_equal(current_owner, desired_owner))

    changes: list[AccessChange] = []
    for auth_user_id, desired_role in sorted(desired_roles.items()):
        current_role = current_roles.get(auth_user_id, "")
        if not current_role:
            changes.append(
                AccessChange(
                    action=CHANGE_ADD,
                    auth_user_id=auth_user_id,
                    after_role=desired_role,
                    reason="missing_direct_assignment",
                )
            )
        elif current_role == desired_role:
            changes.append(
                AccessChange(
                    action=CHANGE_KEEP,
                    auth_user_id=auth_user_id,
                    before_role=current_role,
                    after_role=desired_role,
                    reason="already_current",
                )
            )
        elif current_role == ROLE_OWNER or desired_role == ROLE_OWNER:
            changes.append(
                AccessChange(
                    action=CHANGE_TRANSFER_OWNER,
                    auth_user_id=auth_user_id,
                    before_role=current_role,
                    after_role=desired_role,
                    reason="dedicated_owner_transfer_required",
                )
            )
        else:
            changes.append(
                AccessChange(
                    action=CHANGE_UPDATE,
                    auth_user_id=auth_user_id,
                    before_role=current_role,
                    after_role=desired_role,
                    reason="role_changed_in_app",
                )
            )

    if settings.prune_stale_direct_assignments:
        for auth_user_id, current_role in sorted(current_roles.items()):
            if auth_user_id not in desired_roles:
                if current_role == ROLE_OWNER:
                    owner_transfer_required = True
                    changes.append(
                        AccessChange(
                            action=CHANGE_TRANSFER_OWNER,
                            auth_user_id=auth_user_id,
                            before_role=current_role,
                            after_role="",
                            reason="owner_cannot_be_pruned_without_transfer",
                        )
                    )
                else:
                    changes.append(
                        AccessChange(
                            action=CHANGE_REMOVE,
                            auth_user_id=auth_user_id,
                            before_role=current_role,
                            reason="stale_direct_assignment",
                        )
                    )

    current_fingerprint = build_current_projection_fingerprint(
        projection.chunk_project_id,
        direct,
        projection_version=projection.projection_version,
        source_service=projection.source_service,
    )
    return AccessSyncPlan(
        projection=projection,
        changes=tuple(changes),
        current_projection_fingerprint=current_fingerprint,
        owner_transfer_required=owner_transfer_required,
        current_owner_auth_user_id=current_owner,
        desired_owner_auth_user_id=desired_owner,
        preserved_group_assignments=len(groups) if settings.preserve_group_assignments else 0,
        duplicate_direct_assignments=duplicate_count,
    )


# ---------------------------------------------------------------------------
# Per-project lock and bounded result cache
# ---------------------------------------------------------------------------


_LOCKS_GUARD = threading.RLock()
_PROJECT_LOCKS: dict[str, threading.RLock] = {}
_CACHE_GUARD = threading.RLock()
_SUCCESS_CACHE: "OrderedDict[str, tuple[float, AccessSyncResult]]" = OrderedDict()
_CACHE_TTL_SECONDS = 30.0
_CACHE_MAX_ENTRIES = 512


def _project_lock(chunk_project_id: str) -> threading.RLock:
    with _LOCKS_GUARD:
        lock = _PROJECT_LOCKS.get(chunk_project_id)
        if lock is None:
            lock = threading.RLock()
            _PROJECT_LOCKS[chunk_project_id] = lock
        return lock


def _cache_key(chunk_project_id: str, projection_fingerprint: str) -> str:
    return f"{chunk_project_id}:{projection_fingerprint}"


def _cache_get(chunk_project_id: str, projection_fingerprint: str) -> Optional[AccessSyncResult]:
    key = _cache_key(chunk_project_id, projection_fingerprint)
    now = time.monotonic()
    with _CACHE_GUARD:
        item = _SUCCESS_CACHE.get(key)
        if item is None:
            return None
        expires_at, result = item
        if expires_at <= now:
            _SUCCESS_CACHE.pop(key, None)
            return None
        _SUCCESS_CACHE.move_to_end(key)
        return result


def _cache_put(result: AccessSyncResult) -> None:
    if not result.ok or not result.projection_fingerprint:
        return
    key = _cache_key(result.chunk_project_id, result.projection_fingerprint)
    with _CACHE_GUARD:
        _SUCCESS_CACHE[key] = (time.monotonic() + _CACHE_TTL_SECONDS, result)
        _SUCCESS_CACHE.move_to_end(key)
        while len(_SUCCESS_CACHE) > _CACHE_MAX_ENTRIES:
            _SUCCESS_CACHE.popitem(last=False)


def clear_project_access_cache(chunk_project_id: Any = "") -> None:
    project_id = _safe_text(chunk_project_id, "", 255)
    with _CACHE_GUARD:
        if not project_id:
            _SUCCESS_CACHE.clear()
            return
        prefix = f"{project_id}:"
        for key in list(_SUCCESS_CACHE):
            if key.startswith(prefix):
                _SUCCESS_CACHE.pop(key, None)


# ---------------------------------------------------------------------------
# Reconciliation execution
# ---------------------------------------------------------------------------


def _repository_transaction(repository: Any, *, commit: bool) -> ContextManager[Any]:
    transaction = getattr(repository, "transaction", None)
    if callable(transaction):
        try:
            return transaction(commit=commit)
        except TypeError:
            return transaction()
    return nullcontext()


def _repository_state(repository: Any, project_id: str) -> Mapping[str, Any]:
    method = getattr(repository, "get_project_access_state", None)
    if callable(method):
        try:
            return _as_mapping(method(project_id))
        except Exception:
            return {}
    return {}


def _set_repository_state(
    repository: Any,
    project_id: str,
    *,
    status: str,
    projection: AccessProjection,
    error_code: str = "",
) -> None:
    method = getattr(repository, "set_project_access_state", None)
    if not callable(method):
        return
    method(
        project_id,
        {
            "status": status,
            "projection_fingerprint": projection.projection_fingerprint,
            "projection_version": projection.projection_version,
            "source_service": projection.source_service,
            "assignment_count": projection.assignment_count,
            "owner_fingerprint": _short_fingerprint(projection.owner_auth_user_id, "own"),
            "request_id": projection.request_id,
            "correlation_id": projection.correlation_id,
            "error_code": error_code or None,
            "updated_at": _utcnow_iso(),
        },
    )


def verify_project_access_projection(
    projection: AccessProjection,
    repository: Any,
    *,
    config: Any = None,
) -> tuple[bool, dict[str, Any]]:
    settings = _load_access_settings(config)
    current_records = repository.list_assignments(projection.chunk_project_id)
    direct, groups = _normalize_current_assignments(current_records, settings=settings)
    current_roles, duplicates = _direct_role_map(direct)
    desired_roles = projection.role_map()
    current_owner = _owner_from_role_map(current_roles)
    ok = current_roles == desired_roles and _constant_equal(current_owner, projection.owner_auth_user_id)
    return ok, {
        "desired_assignment_count": len(desired_roles),
        "current_assignment_count": len(current_roles),
        "preserved_group_assignments": len(groups),
        "duplicate_direct_assignments": duplicates,
        "current_fingerprint": build_current_projection_fingerprint(
            projection.chunk_project_id,
            direct,
            projection_version=projection.projection_version,
            source_service=projection.source_service,
        ),
        "desired_fingerprint": projection.projection_fingerprint,
        "owner_matches": bool(current_owner and _constant_equal(current_owner, projection.owner_auth_user_id)),
    }


def _failed_sync_result(
    error: ProjectAccessError,
    *,
    chunk_project_id: str,
    projection_fingerprint: str = "",
    request_id: str = "",
    correlation_id: str = "",
    elapsed_ms: float = 0.0,
) -> AccessSyncResult:
    return AccessSyncResult(
        ok=False,
        status=SYNC_STATUS_REPAIR_REQUIRED if error.repair_required else SYNC_STATUS_FAILED,
        code=error.code,
        chunk_project_id=chunk_project_id,
        status_code=error.status_code,
        projection_fingerprint=projection_fingerprint,
        retryable=error.retryable,
        repair_required=error.repair_required,
        error=str(error),
        request_id=error.request_id or request_id,
        correlation_id=error.correlation_id or correlation_id,
        elapsed_ms=elapsed_ms,
        details=error.details,
    )


def sync_project_access_projection(
    chunk_project_id: Any,
    payload: Any,
    *,
    repository: Any = None,
    session: Any = None,
    assignment_model: Any = None,
    project_model: Any = None,
    field_map: Optional[Mapping[str, Any]] = None,
    principal: Any = None,
    request_id: str = "",
    correlation_id: str = "",
    idempotency_key: str = "",
    commit: bool = True,
    dry_run: bool = False,
    force: bool = False,
    raise_on_error: bool = False,
    config: Any = None,
) -> AccessSyncResult:
    started = time.perf_counter()
    project_id = _safe_text(chunk_project_id, "", 255)
    projection: Optional[AccessProjection] = None
    repo: Any = None
    try:
        settings = _load_access_settings(config)
        project_id = normalize_chunk_project_id(chunk_project_id)
        if not settings.enabled:
            return AccessSyncResult(
                ok=True,
                status=SYNC_STATUS_DISABLED,
                code=CODE_DISABLED,
                chunk_project_id=project_id,
                idempotent=True,
                verified=True,
                request_id=_safe_text(request_id, "", 160),
                correlation_id=_safe_text(correlation_id or request_id, "", 160),
                elapsed_ms=(time.perf_counter() - started) * 1000.0,
            )
        if not settings.runtime_business_mutations_enabled and not dry_run:
            raise ProjectAccessError(
                "Runtime business mutations are disabled.",
                code=CODE_ACCESS_DENIED,
                status_code=503,
                retryable=True,
            )

        resolved_principal = require_project_access_service_principal(
            principal=principal,
            allowed_service_ids=(settings.source_service, "vectoplan-chunk-init"),
            config=config,
        )
        projection = build_access_projection(
            project_id,
            payload,
            principal=resolved_principal,
            request_id=request_id,
            correlation_id=correlation_id,
            idempotency_key=idempotency_key,
            config=config,
        )
        repo = resolve_project_access_repository(
            repository,
            session=session,
            assignment_model=assignment_model,
            project_model=project_model,
            field_map=field_map,
        )

        with _project_lock(project_id):
            if force:
                clear_project_access_cache(project_id)
            if not force and not dry_run:
                cached = _cache_get(project_id, projection.projection_fingerprint)
                state = _repository_state(repo, project_id)
                state_fingerprint = _safe_text(
                    state.get("projection_fingerprint") or state.get("state_fingerprint"),
                    "",
                    128,
                )
                state_status = _safe_text(state.get("status") or state.get("state_status"), "", 40)
                if cached is not None and state_status == SYNC_STATUS_READY and _constant_equal(
                    state_fingerprint,
                    projection.projection_fingerprint,
                ):
                    return replace(
                        cached,
                        idempotent=True,
                        request_id=projection.request_id,
                        correlation_id=projection.correlation_id,
                        elapsed_ms=(time.perf_counter() - started) * 1000.0,
                    )

            current = repo.list_assignments(project_id)
            plan = plan_project_access_reconciliation(projection, current, config=config)
            if plan.owner_transfer_required:
                raise ProjectAccessError(
                    "The persisted owner differs from the desired owner. Use the dedicated owner-transfer operation.",
                    code=CODE_OWNER_TRANSFER_REQUIRED,
                    status_code=409,
                    repair_required=True,
                    details={
                        "current_owner_fingerprint": _short_fingerprint(plan.current_owner_auth_user_id, "own"),
                        "desired_owner_fingerprint": _short_fingerprint(plan.desired_owner_auth_user_id, "own"),
                    },
                    request_id=projection.request_id,
                    correlation_id=projection.correlation_id,
                )

            if dry_run:
                return AccessSyncResult(
                    ok=True,
                    status=SYNC_STATUS_PENDING if not plan.no_changes else SYNC_STATUS_READY,
                    code=CODE_OK,
                    chunk_project_id=project_id,
                    projection_fingerprint=projection.projection_fingerprint,
                    applied_changes=tuple(item for item in plan.changes if item.mutates),
                    assignment_count=projection.assignment_count,
                    preserved_group_assignments=plan.preserved_group_assignments,
                    idempotent=plan.no_changes,
                    verified=False,
                    request_id=projection.request_id,
                    correlation_id=projection.correlation_id,
                    elapsed_ms=(time.perf_counter() - started) * 1000.0,
                    details={"dry_run": True, "plan": plan.to_dict(include_private=False)},
                )

            applied: list[AccessChange] = []
            with _repository_transaction(repo, commit=commit):
                _set_repository_state(repo, project_id, status=SYNC_STATUS_SYNCING, projection=projection)
                for change in plan.changes:
                    if not change.mutates:
                        continue
                    if change.action == CHANGE_TRANSFER_OWNER:
                        raise ProjectAccessError(
                            "Owner transfer must use transfer_project_owner().",
                            code=CODE_OWNER_TRANSFER_REQUIRED,
                            status_code=409,
                            repair_required=True,
                        )
                    if change.action in {CHANGE_ADD, CHANGE_UPDATE}:
                        role = change.after_role
                        repo.upsert_direct_assignment(
                            project_id,
                            AccessAssignment(
                                auth_user_id=change.auth_user_id,
                                role=role,
                                source_service=projection.source_service,
                                metadata={
                                    "projection_version": projection.projection_version,
                                    "projection_fingerprint": projection.projection_fingerprint,
                                },
                            ),
                        )
                        applied.append(change)
                    elif change.action == CHANGE_REMOVE:
                        repo.delete_direct_assignment(project_id, change.auth_user_id)
                        applied.append(change)

                verified = True
                verification_details: dict[str, Any] = {}
                if settings.verify_after_sync:
                    verified, verification_details = verify_project_access_projection(
                        projection,
                        repo,
                        config=config,
                    )
                    if not verified:
                        raise ProjectAccessError(
                            "The persisted access projection does not match the app projection after synchronization.",
                            code=CODE_VERIFICATION_FAILED,
                            status_code=409,
                            repair_required=True,
                            details=verification_details,
                        )
                _set_repository_state(repo, project_id, status=SYNC_STATUS_READY, projection=projection)
                flush = getattr(repo, "flush", None)
                if callable(flush):
                    flush()

            result = AccessSyncResult(
                ok=True,
                status=SYNC_STATUS_READY,
                code=CODE_OK,
                chunk_project_id=project_id,
                projection_fingerprint=projection.projection_fingerprint,
                applied_changes=tuple(applied),
                assignment_count=projection.assignment_count,
                preserved_group_assignments=plan.preserved_group_assignments,
                idempotent=not applied,
                verified=verified,
                request_id=projection.request_id,
                correlation_id=projection.correlation_id,
                elapsed_ms=(time.perf_counter() - started) * 1000.0,
                details={"verification": verification_details} if verification_details else {},
            )
            _cache_put(result)
            return result
    except ProjectAccessError as exc:
        if repo is not None and projection is not None:
            try:
                _set_repository_state(
                    repo,
                    project_id,
                    status=SYNC_STATUS_REPAIR_REQUIRED if exc.repair_required else SYNC_STATUS_FAILED,
                    projection=projection,
                    error_code=exc.code,
                )
                flush = getattr(repo, "flush", None)
                if callable(flush):
                    flush()
                if commit:
                    commit_method = getattr(repo, "commit", None)
                    if callable(commit_method):
                        commit_method()
            except Exception:
                pass
        result = _failed_sync_result(
            exc,
            chunk_project_id=project_id,
            projection_fingerprint=projection.projection_fingerprint if projection else "",
            request_id=projection.request_id if projection else request_id,
            correlation_id=projection.correlation_id if projection else correlation_id,
            elapsed_ms=(time.perf_counter() - started) * 1000.0,
        )
        if raise_on_error:
            raise
        return result
    except Exception as exc:
        error = ProjectAccessError(
            "Unexpected project access synchronization failure.",
            code=CODE_INTERNAL_ERROR,
            status_code=500,
            retryable=True,
            details={"error_type": type(exc).__name__},
            request_id=projection.request_id if projection else request_id,
            correlation_id=projection.correlation_id if projection else correlation_id,
        )
        result = _failed_sync_result(
            error,
            chunk_project_id=project_id,
            projection_fingerprint=projection.projection_fingerprint if projection else "",
            elapsed_ms=(time.perf_counter() - started) * 1000.0,
        )
        if raise_on_error:
            raise error from exc
        return result


def initialize_project_access(
    chunk_project_id: Any,
    payload: Any,
    **kwargs: Any,
) -> AccessSyncResult:
    """Initialize a new Chunk project access projection.

    Existing matching assignments are idempotent.  An existing different owner
    still requires the dedicated transfer operation.
    """
    return sync_project_access_projection(chunk_project_id, payload, **kwargs)


# ---------------------------------------------------------------------------
# Dedicated owner transfer
# ---------------------------------------------------------------------------


def transfer_project_owner(
    chunk_project_id: Any,
    *,
    new_owner_auth_user_id: Any,
    old_owner_auth_user_id: Any = "",
    former_owner_role: str = ROLE_ADMIN,
    repository: Any = None,
    session: Any = None,
    assignment_model: Any = None,
    project_model: Any = None,
    field_map: Optional[Mapping[str, Any]] = None,
    principal: Any = None,
    request_id: str = "",
    correlation_id: str = "",
    commit: bool = True,
    raise_on_error: bool = False,
    config: Any = None,
) -> OwnerTransferResult:
    started = time.perf_counter()
    project_id = _safe_text(chunk_project_id, "", 255)
    resolved_request_id = _safe_text(request_id, "", 160)
    resolved_correlation_id = _safe_text(correlation_id or request_id, "", 160)
    try:
        settings = _load_access_settings(config)
        project_id = normalize_chunk_project_id(chunk_project_id)
        if not settings.enabled:
            return OwnerTransferResult(
                ok=True,
                status=SYNC_STATUS_DISABLED,
                code=CODE_DISABLED,
                chunk_project_id=project_id,
                verified=True,
                request_id=resolved_request_id,
                correlation_id=resolved_correlation_id,
                elapsed_ms=(time.perf_counter() - started) * 1000.0,
            )
        if not settings.runtime_business_mutations_enabled:
            raise ProjectAccessError(
                "Runtime business mutations are disabled.",
                code=CODE_ACCESS_DENIED,
                status_code=503,
                retryable=True,
            )
        resolved_principal = require_project_access_service_principal(
            principal=principal,
            allowed_service_ids=(settings.source_service, "vectoplan-chunk-init"),
            config=config,
        )
        resolved_request_id = _safe_text(request_id or _principal_request_id(resolved_principal), "", 160)
        resolved_correlation_id = _safe_text(
            correlation_id or _principal_correlation_id(resolved_principal) or resolved_request_id,
            "",
            160,
        )
        new_owner = normalize_auth_user_id(
            new_owner_auth_user_id,
            strict=settings.strict_canonical_user_ids,
        )
        expected_old_owner = ""
        if old_owner_auth_user_id not in (None, ""):
            expected_old_owner = normalize_auth_user_id(
                old_owner_auth_user_id,
                strict=settings.strict_canonical_user_ids,
            )
        if expected_old_owner and _constant_equal(expected_old_owner, new_owner):
            raise ProjectAccessError(
                "The new owner must differ from the old owner.",
                code=CODE_OWNER_TRANSFER_INVALID,
                status_code=400,
            )
        demotion_role = _safe_text(former_owner_role, ROLE_ADMIN, 40).lower()
        remove_former_owner = demotion_role in {"", "remove", "deleted", "none"}
        if not remove_former_owner:
            demotion_role = normalize_role(demotion_role, allow_owner=False)

        repo = resolve_project_access_repository(
            repository,
            session=session,
            assignment_model=assignment_model,
            project_model=project_model,
            field_map=field_map,
        )
        with _project_lock(project_id):
            direct, _groups = _normalize_current_assignments(
                repo.list_assignments(project_id),
                settings=settings,
            )
            current_roles, _duplicates = _direct_role_map(direct)
            current_owner = _owner_from_role_map(current_roles)
            if not current_owner:
                raise ProjectAccessError(
                    "The current projection has no owner and requires repair.",
                    code=CODE_OWNER_REQUIRED,
                    status_code=409,
                    repair_required=True,
                )
            if expected_old_owner and not _constant_equal(current_owner, expected_old_owner):
                raise ProjectAccessError(
                    "The expected old owner does not match the persisted owner.",
                    code=CODE_PROJECTION_STALE,
                    status_code=409,
                    repair_required=True,
                    details={
                        "expected_owner_fingerprint": _short_fingerprint(expected_old_owner, "own"),
                        "persisted_owner_fingerprint": _short_fingerprint(current_owner, "own"),
                    },
                )
            if _constant_equal(current_owner, new_owner):
                return OwnerTransferResult(
                    ok=True,
                    status=SYNC_STATUS_READY,
                    code=CODE_OK,
                    chunk_project_id=project_id,
                    old_owner_fingerprint=_short_fingerprint(current_owner, "own"),
                    new_owner_fingerprint=_short_fingerprint(new_owner, "own"),
                    former_owner_role=current_roles.get(current_owner, ROLE_OWNER),
                    verified=True,
                    request_id=resolved_request_id,
                    correlation_id=resolved_correlation_id,
                    elapsed_ms=(time.perf_counter() - started) * 1000.0,
                    details={"idempotent": True},
                )

            with _repository_transaction(repo, commit=commit):
                repo.upsert_direct_assignment(
                    project_id,
                    AccessAssignment(
                        auth_user_id=new_owner,
                        role=ROLE_OWNER,
                        source_service=settings.source_service,
                        metadata={"operation": "owner_transfer"},
                    ),
                )
                if remove_former_owner:
                    repo.delete_direct_assignment(project_id, current_owner)
                else:
                    repo.upsert_direct_assignment(
                        project_id,
                        AccessAssignment(
                            auth_user_id=current_owner,
                            role=demotion_role,
                            source_service=settings.source_service,
                            metadata={"operation": "owner_transfer_demotion"},
                        ),
                    )
                flush = getattr(repo, "flush", None)
                if callable(flush):
                    flush()

                verify_direct, _ = _normalize_current_assignments(
                    repo.list_assignments(project_id),
                    settings=settings,
                )
                verified_roles, _ = _direct_role_map(verify_direct)
                verified_owner = _owner_from_role_map(verified_roles)
                verified = _constant_equal(verified_owner, new_owner)
                if not verified:
                    raise ProjectAccessError(
                        "Owner transfer verification failed.",
                        code=CODE_VERIFICATION_FAILED,
                        status_code=409,
                        repair_required=True,
                    )

                projection_fingerprint = build_current_projection_fingerprint(
                    project_id,
                    verify_direct,
                    projection_version=settings.projection_version,
                    source_service=settings.source_service,
                )
                state_method = getattr(repo, "set_project_access_state", None)
                if callable(state_method):
                    state_method(
                        project_id,
                        {
                            "status": SYNC_STATUS_READY,
                            "projection_fingerprint": projection_fingerprint,
                            "projection_version": settings.projection_version,
                            "source_service": settings.source_service,
                            "owner_fingerprint": _short_fingerprint(new_owner, "own"),
                            "request_id": resolved_request_id,
                            "correlation_id": resolved_correlation_id,
                            "updated_at": _utcnow_iso(),
                        },
                    )

            clear_project_access_cache(project_id)
            return OwnerTransferResult(
                ok=True,
                status=SYNC_STATUS_READY,
                code=CODE_OK,
                chunk_project_id=project_id,
                old_owner_fingerprint=_short_fingerprint(current_owner, "own"),
                new_owner_fingerprint=_short_fingerprint(new_owner, "own"),
                former_owner_role="removed" if remove_former_owner else demotion_role,
                verified=True,
                request_id=resolved_request_id,
                correlation_id=resolved_correlation_id,
                elapsed_ms=(time.perf_counter() - started) * 1000.0,
            )
    except ProjectAccessError as exc:
        result = OwnerTransferResult(
            ok=False,
            status=SYNC_STATUS_REPAIR_REQUIRED if exc.repair_required else SYNC_STATUS_FAILED,
            code=exc.code,
            chunk_project_id=project_id,
            status_code=exc.status_code,
            retryable=exc.retryable,
            repair_required=exc.repair_required,
            error=str(exc),
            request_id=exc.request_id or resolved_request_id,
            correlation_id=exc.correlation_id or resolved_correlation_id,
            elapsed_ms=(time.perf_counter() - started) * 1000.0,
            details=exc.details,
        )
        if raise_on_error:
            raise
        return result
    except Exception as exc:
        error = ProjectAccessError(
            "Unexpected owner transfer failure.",
            code=CODE_INTERNAL_ERROR,
            status_code=500,
            retryable=True,
            details={"error_type": type(exc).__name__},
            request_id=resolved_request_id,
            correlation_id=resolved_correlation_id,
        )
        if raise_on_error:
            raise error from exc
        return OwnerTransferResult(
            ok=False,
            status=SYNC_STATUS_FAILED,
            code=error.code,
            chunk_project_id=project_id,
            status_code=error.status_code,
            retryable=True,
            error=str(error),
            request_id=resolved_request_id,
            correlation_id=resolved_correlation_id,
            elapsed_ms=(time.perf_counter() - started) * 1000.0,
            details=error.details,
        )


# ---------------------------------------------------------------------------
# Authorization decisions
# ---------------------------------------------------------------------------


def _best_role(assignments: Iterable[AccessAssignment], auth_user_id: str) -> tuple[str, str]:
    candidates: list[tuple[int, str, str]] = []
    for assignment in assignments:
        if not assignment.active:
            continue
        if assignment.is_direct and _constant_equal(assignment.auth_user_id, auth_user_id):
            candidates.append((ROLE_RANK.get(assignment.role, 0), assignment.role, "direct"))
    if not candidates:
        return "", "none"
    candidates.sort(reverse=True)
    return candidates[0][1], candidates[0][2]


def authorize_project_operation(
    chunk_project_id: Any,
    operation: Any,
    *,
    auth_user_id: Any = "",
    repository: Any = None,
    session: Any = None,
    assignment_model: Any = None,
    project_model: Any = None,
    field_map: Optional[Mapping[str, Any]] = None,
    principal: Any = None,
    public: bool = False,
    public_read_only_verified: bool = False,
    request_id: str = "",
    correlation_id: str = "",
    config: Any = None,
) -> AccessDecision:
    project_id = _safe_text(chunk_project_id, "", 255)
    normalized_operation = _safe_text(operation, "", 120)
    try:
        settings = _load_access_settings(config)
        project_id = normalize_chunk_project_id(chunk_project_id)
        normalized_operation = normalize_operation(operation)
        resolved_principal = _resolve_service_principal(principal)
        resolved_request_id = _safe_text(request_id or _principal_request_id(resolved_principal), "", 160)
        resolved_correlation_id = _safe_text(
            correlation_id or _principal_correlation_id(resolved_principal) or resolved_request_id,
            "",
            160,
        )

        if not settings.enabled:
            return AccessDecision(
                allowed=not settings.default_deny,
                code=CODE_DISABLED,
                chunk_project_id=project_id,
                operation=normalized_operation,
                status_code=200 if not settings.default_deny else 403,
                role="",
                read_only=normalized_operation in READ_OPERATIONS,
                public=public,
                source="disabled",
                reason="Project access control is disabled.",
                request_id=resolved_request_id,
                correlation_id=resolved_correlation_id,
            )

        if settings.service_auth_required:
            require_project_access_service_principal(
                principal=resolved_principal,
                allowed_service_ids=_DEFAULT_DECISION_SERVICES,
                config=config,
            )

        if public:
            if normalized_operation in MUTATION_OPERATIONS or not settings.allow_public_mutations:
                if normalized_operation in MUTATION_OPERATIONS:
                    return AccessDecision(
                        allowed=False,
                        code=CODE_PUBLIC_MUTATION_DENIED,
                        chunk_project_id=project_id,
                        operation=normalized_operation,
                        status_code=403,
                        role=ROLE_VIEWER,
                        read_only=True,
                        public=True,
                        source="public",
                        reason="Public project access is read-only.",
                        request_id=resolved_request_id,
                        correlation_id=resolved_correlation_id,
                    )
            if not public_read_only_verified:
                return AccessDecision(
                    allowed=False,
                    code=CODE_PUBLIC_READ_NOT_VERIFIED,
                    chunk_project_id=project_id,
                    operation=normalized_operation,
                    status_code=403,
                    role=ROLE_VIEWER,
                    read_only=True,
                    public=True,
                    source="public",
                    reason="Public read-only access has not been verified by the server.",
                    request_id=resolved_request_id,
                    correlation_id=resolved_correlation_id,
                )
            allowed = normalized_operation in settings.viewer_allowed_operations and normalized_operation in READ_OPERATIONS
            return AccessDecision(
                allowed=allowed,
                code=CODE_OK if allowed else CODE_ACCESS_DENIED,
                chunk_project_id=project_id,
                operation=normalized_operation,
                status_code=200 if allowed else 403,
                role=ROLE_VIEWER,
                read_only=True,
                public=True,
                source="public_verified",
                reason="Public read-only access allowed." if allowed else "The operation is not available to public viewers.",
                request_id=resolved_request_id,
                correlation_id=resolved_correlation_id,
            )

        canonical_id = normalize_auth_user_id(
            auth_user_id,
            strict=settings.strict_canonical_user_ids,
        )
        repo = resolve_project_access_repository(
            repository,
            session=session,
            assignment_model=assignment_model,
            project_model=project_model,
            field_map=field_map,
        )
        direct, groups = _normalize_current_assignments(
            repo.list_assignments(project_id),
            settings=settings,
        )
        role, source = _best_role(direct, canonical_id)
        if not role:
            return AccessDecision(
                allowed=False,
                code=CODE_ACCESS_DENIED,
                chunk_project_id=project_id,
                operation=normalized_operation,
                status_code=403,
                read_only=True,
                public=False,
                source="none",
                reason="No project assignment exists for the canonical auth user.",
                auth_user_id=canonical_id,
                request_id=resolved_request_id,
                correlation_id=resolved_correlation_id,
                details={"preserved_group_assignments": len(groups)},
            )
        allowed = role_allows_operation(
            role,
            normalized_operation,
            viewer_allowed_operations=settings.viewer_allowed_operations,
            viewer_denied_operations=settings.viewer_denied_operations,
        )
        if role == ROLE_VIEWER and settings.viewer_read_only and normalized_operation in MUTATION_OPERATIONS:
            allowed = False
        return AccessDecision(
            allowed=allowed,
            code=CODE_OK if allowed else CODE_ACCESS_DENIED,
            chunk_project_id=project_id,
            operation=normalized_operation,
            status_code=200 if allowed else 403,
            role=role,
            read_only=role == ROLE_VIEWER or normalized_operation in READ_OPERATIONS,
            public=False,
            source=source,
            reason="Project operation allowed." if allowed else "The project role does not allow this operation.",
            auth_user_id=canonical_id,
            request_id=resolved_request_id,
            correlation_id=resolved_correlation_id,
            details={"capabilities": list(role_capabilities(role))},
        )
    except ProjectAccessError as exc:
        return AccessDecision(
            allowed=False,
            code=exc.code,
            chunk_project_id=project_id,
            operation=normalized_operation,
            status_code=exc.status_code,
            read_only=True,
            public=public,
            reason=str(exc),
            request_id=exc.request_id or _safe_text(request_id, "", 160),
            correlation_id=exc.correlation_id or _safe_text(correlation_id or request_id, "", 160),
            details=exc.details,
        )
    except Exception as exc:
        return AccessDecision(
            allowed=False,
            code=CODE_INTERNAL_ERROR,
            chunk_project_id=project_id,
            operation=normalized_operation,
            status_code=500,
            read_only=True,
            public=public,
            reason="Project access authorization failed closed.",
            request_id=_safe_text(request_id, "", 160),
            correlation_id=_safe_text(correlation_id or request_id, "", 160),
            details={"error_type": type(exc).__name__},
        )


def require_project_operation(*args: Any, **kwargs: Any) -> AccessDecision:
    return authorize_project_operation(*args, **kwargs).require_allowed()


def get_project_role(
    chunk_project_id: Any,
    auth_user_id: Any,
    *,
    repository: Any = None,
    session: Any = None,
    assignment_model: Any = None,
    project_model: Any = None,
    field_map: Optional[Mapping[str, Any]] = None,
    config: Any = None,
) -> str:
    settings = _load_access_settings(config)
    project_id = normalize_chunk_project_id(chunk_project_id)
    canonical_id = normalize_auth_user_id(
        auth_user_id,
        strict=settings.strict_canonical_user_ids,
    )
    repo = resolve_project_access_repository(
        repository,
        session=session,
        assignment_model=assignment_model,
        project_model=project_model,
        field_map=field_map,
    )
    direct, _groups = _normalize_current_assignments(repo.list_assignments(project_id), settings=settings)
    role, _source = _best_role(direct, canonical_id)
    return role


def list_project_access_assignments(
    chunk_project_id: Any,
    *,
    repository: Any = None,
    session: Any = None,
    assignment_model: Any = None,
    project_model: Any = None,
    field_map: Optional[Mapping[str, Any]] = None,
    include_groups: bool = True,
    config: Any = None,
) -> list[AccessAssignment]:
    """Return normalized assignments without changing persistence."""
    settings = _load_access_settings(config)
    project_id = normalize_chunk_project_id(chunk_project_id)
    repo = resolve_project_access_repository(
        repository,
        session=session,
        assignment_model=assignment_model,
        project_model=project_model,
        field_map=field_map,
    )
    direct, groups = _normalize_current_assignments(repo.list_assignments(project_id), settings=settings)
    return direct + groups if include_groups else direct


def get_project_access_status(
    chunk_project_id: Any,
    *,
    repository: Any = None,
    session: Any = None,
    assignment_model: Any = None,
    project_model: Any = None,
    field_map: Optional[Mapping[str, Any]] = None,
    include_assignments: bool = False,
    include_private: bool = False,
    config: Any = None,
) -> dict[str, Any]:
    """Build a bounded, redacted project access status snapshot."""
    settings = _load_access_settings(config)
    project_id = normalize_chunk_project_id(chunk_project_id)
    repo = resolve_project_access_repository(
        repository,
        session=session,
        assignment_model=assignment_model,
        project_model=project_model,
        field_map=field_map,
    )
    state = _repository_state(repo, project_id)
    try:
        direct, groups = _normalize_current_assignments(repo.list_assignments(project_id), settings=settings)
        role_map, duplicates = _direct_role_map(direct)
        owner = _owner_from_role_map(role_map)
        status = _safe_text(state.get("status") or state.get("state_status"), "", 40)
        if not settings.enabled:
            status = SYNC_STATUS_DISABLED
        elif duplicates or not owner:
            status = SYNC_STATUS_REPAIR_REQUIRED
        elif status not in SYNC_STATUSES:
            status = SYNC_STATUS_READY
        fingerprint = build_current_projection_fingerprint(
            project_id,
            direct,
            projection_version=_safe_text(
                state.get("projection_version") or state.get("state_version"),
                settings.projection_version,
                120,
            ),
            source_service=settings.source_service,
        )
        payload: dict[str, Any] = {
            "ok": status not in {SYNC_STATUS_FAILED, SYNC_STATUS_REPAIR_REQUIRED},
            "status": status,
            "chunk_project_id": project_id,
            "chunkProjectId": project_id,
            "projection_fingerprint": fingerprint,
            "projectionFingerprint": fingerprint,
            "stored_projection_fingerprint": _safe_text(
                state.get("projection_fingerprint") or state.get("state_fingerprint"),
                "",
                128,
            ) or None,
            "storedProjectionFingerprint": _safe_text(
                state.get("projection_fingerprint") or state.get("state_fingerprint"),
                "",
                128,
            ) or None,
            "projection_version": _safe_text(
                state.get("projection_version") or state.get("state_version"),
                settings.projection_version,
                120,
            ),
            "projectionVersion": _safe_text(
                state.get("projection_version") or state.get("state_version"),
                settings.projection_version,
                120,
            ),
            "owner_fingerprint": _short_fingerprint(owner, "own") or None,
            "ownerFingerprint": _short_fingerprint(owner, "own") or None,
            "direct_assignment_count": len(role_map),
            "directAssignmentCount": len(role_map),
            "group_assignment_count": len(groups),
            "groupAssignmentCount": len(groups),
            "duplicate_direct_assignments": duplicates,
            "duplicateDirectAssignments": duplicates,
            "viewer_read_only": settings.viewer_read_only,
            "viewerReadOnly": settings.viewer_read_only,
            "repair_required": status == SYNC_STATUS_REPAIR_REQUIRED,
            "repairRequired": status == SYNC_STATUS_REPAIR_REQUIRED,
            "updated_at": _safe_text(state.get("updated_at") or state.get("state_updated_at"), "", 100) or None,
            "updatedAt": _safe_text(state.get("updated_at") or state.get("state_updated_at"), "", 100) or None,
        }
        if include_private:
            payload["owner_auth_user_id"] = owner or None
            payload["ownerAuthUserId"] = owner or None
        if include_assignments:
            payload["assignments"] = [
                item.to_dict(include_private=include_private)
                for item in sorted(direct + groups, key=lambda entry: (entry.assignment_type, -ROLE_RANK.get(entry.role, 0), entry.auth_user_id or entry.group_id))
            ]
        return payload
    except ProjectAccessError as exc:
        return {
            "ok": False,
            "status": SYNC_STATUS_REPAIR_REQUIRED if exc.repair_required else SYNC_STATUS_FAILED,
            "code": exc.code,
            "chunk_project_id": project_id,
            "chunkProjectId": project_id,
            "repair_required": exc.repair_required,
            "repairRequired": exc.repair_required,
            "error": str(exc),
            "details": exc.details,
        }


# ---------------------------------------------------------------------------
# Serialization and diagnostics
# ---------------------------------------------------------------------------


def serialize_access_assignment(value: Any, *, include_private: bool = False) -> dict[str, Any]:
    if isinstance(value, AccessAssignment):
        return value.to_dict(include_private=include_private)
    return {}


def serialize_access_projection(value: Any, *, include_private: bool = False) -> dict[str, Any]:
    if isinstance(value, AccessProjection):
        return value.to_dict(include_private=include_private)
    return {}


def serialize_access_sync_plan(value: Any, *, include_private: bool = False) -> dict[str, Any]:
    if isinstance(value, AccessSyncPlan):
        return value.to_dict(include_private=include_private)
    return {}


def serialize_access_sync_result(value: Any, *, include_private: bool = False) -> dict[str, Any]:
    if isinstance(value, AccessSyncResult):
        return value.to_dict(include_private=include_private)
    return {}


def serialize_access_decision(value: Any, *, include_private: bool = False) -> dict[str, Any]:
    if isinstance(value, AccessDecision):
        return value.to_dict(include_private=include_private)
    return {}


def serialize_owner_transfer_result(value: Any) -> dict[str, Any]:
    if isinstance(value, OwnerTransferResult):
        return value.to_dict()
    return {}


def get_project_access_service_status(*, config: Any = None) -> dict[str, Any]:
    try:
        settings = _load_access_settings(config)
        with _CACHE_GUARD:
            cache_entries = len(_SUCCESS_CACHE)
        with _LOCKS_GUARD:
            lock_entries = len(_PROJECT_LOCKS)
        return {
            "ok": True,
            "service": "vectoplan-chunk",
            "component": "project_access_service",
            "version": PROJECT_ACCESS_SERVICE_VERSION,
            "schema_version": PROJECT_ACCESS_SCHEMA_VERSION,
            "enabled": settings.enabled,
            "default_deny": settings.default_deny,
            "strict_canonical_user_ids": settings.strict_canonical_user_ids,
            "canonical_user_id_field": settings.canonical_user_id_field,
            "allowed_roles": list(settings.allowed_roles),
            "role_capabilities": {
                role: list(DEFAULT_ROLE_CAPABILITIES[role])
                for role in ACCESS_ROLES
            },
            "viewer_read_only": settings.viewer_read_only,
            "viewer_allowed_operations": list(settings.viewer_allowed_operations),
            "viewer_denied_operations": list(settings.viewer_denied_operations),
            "allow_public_mutations": settings.allow_public_mutations,
            "allow_identity_override": settings.allow_identity_override,
            "prune_stale_direct_assignments": settings.prune_stale_direct_assignments,
            "preserve_group_assignments": settings.preserve_group_assignments,
            "verify_after_sync": settings.verify_after_sync,
            "source_service": settings.source_service,
            "projection_version": settings.projection_version,
            "max_direct_assignments": settings.max_direct_assignments,
            "runtime_business_mutations_enabled": settings.runtime_business_mutations_enabled,
            "service_auth_required": settings.service_auth_required,
            "cache": {
                "entries": cache_entries,
                "maximum": _CACHE_MAX_ENTRIES,
                "ttl_seconds": _CACHE_TTL_SECONDS,
            },
            "project_locks": lock_entries,
            "persistence": {
                "repository_injected": True,
                "outbound_calls": False,
                "schema_mutations": False,
            },
        }
    except Exception as exc:
        return {
            "ok": False,
            "service": "vectoplan-chunk",
            "component": "project_access_service",
            "version": PROJECT_ACCESS_SERVICE_VERSION,
            "error": "Project access service status could not be built.",
            "error_type": type(exc).__name__,
        }


__all__ = [
    "PROJECT_ACCESS_SERVICE_VERSION",
    "PROJECT_ACCESS_SCHEMA_VERSION",
    "PROJECT_ACCESS_PROJECTION_SCHEMA_VERSION",
    "PROJECT_ACCESS_PLAN_SCHEMA_VERSION",
    "PROJECT_ACCESS_RESULT_SCHEMA_VERSION",
    "PROJECT_ACCESS_DECISION_SCHEMA_VERSION",
    "PROJECT_OWNER_TRANSFER_SCHEMA_VERSION",
    "ROLE_OWNER",
    "ROLE_ADMIN",
    "ROLE_EDITOR",
    "ROLE_VIEWER",
    "ACCESS_ROLES",
    "ROLE_RANK",
    "ASSIGNMENT_TYPE_DIRECT",
    "ASSIGNMENT_TYPE_GROUP",
    "ASSIGNMENT_TYPES",
    "SYNC_STATUS_READY",
    "SYNC_STATUS_SYNCING",
    "SYNC_STATUS_PENDING",
    "SYNC_STATUS_FAILED",
    "SYNC_STATUS_REPAIR_REQUIRED",
    "SYNC_STATUS_DISABLED",
    "SYNC_STATUSES",
    "CHANGE_ADD",
    "CHANGE_UPDATE",
    "CHANGE_REMOVE",
    "CHANGE_KEEP",
    "CHANGE_TRANSFER_OWNER",
    "OP_PROJECT_READ",
    "OP_WORLD_READ",
    "OP_BLOCKS_READ",
    "OP_CHUNKS_READ",
    "OP_CHUNKS_BATCH_READ",
    "OP_COMMANDS_EXECUTE",
    "OP_CHUNKS_MATERIALIZE",
    "OP_CHUNKS_WRITE",
    "OP_PROJECT_MANAGE",
    "OP_ACCESS_MANAGE",
    "OP_WORLD_MUTATE",
    "OP_OWNER_TRANSFER",
    "READ_OPERATIONS",
    "MUTATION_OPERATIONS",
    "ALL_OPERATIONS",
    "DEFAULT_VIEWER_ALLOWED_OPERATIONS",
    "DEFAULT_VIEWER_DENIED_OPERATIONS",
    "DEFAULT_ROLE_CAPABILITIES",
    "CODE_OK",
    "CODE_DISABLED",
    "CODE_INVALID_PROJECT",
    "CODE_INVALID_PROJECTION",
    "CODE_CANONICAL_ID_REQUIRED",
    "CODE_LOCAL_ID_REJECTED",
    "CODE_IDENTITY_OVERRIDE_DENIED",
    "CODE_ROLE_INVALID",
    "CODE_OWNER_REQUIRED",
    "CODE_MULTIPLE_OWNERS",
    "CODE_OWNER_TRANSFER_REQUIRED",
    "CODE_OWNER_TRANSFER_INVALID",
    "CODE_ASSIGNMENT_LIMIT",
    "CODE_DUPLICATE_ASSIGNMENT",
    "CODE_REPOSITORY_REQUIRED",
    "CODE_REPOSITORY_ERROR",
    "CODE_VERIFICATION_FAILED",
    "CODE_SERVICE_AUTH_REQUIRED",
    "CODE_SERVICE_NOT_ALLOWED",
    "CODE_ACCESS_DENIED",
    "CODE_PUBLIC_MUTATION_DENIED",
    "CODE_PUBLIC_READ_NOT_VERIFIED",
    "CODE_OPERATION_INVALID",
    "CODE_PROJECTION_STALE",
    "CODE_INTERNAL_ERROR",
    "ProjectAccessError",
    "AccessAssignment",
    "AccessProjection",
    "AccessChange",
    "AccessSyncPlan",
    "AccessSyncResult",
    "AccessDecision",
    "OwnerTransferResult",
    "ProjectAccessRepository",
    "InMemoryProjectAccessRepository",
    "SQLAlchemyProjectAccessRepository",
    "redact_access_text",
    "sanitize_access_value",
    "sanitize_access_mapping",
    "normalize_chunk_project_id",
    "normalize_auth_user_id",
    "normalize_role",
    "normalize_operation",
    "role_capabilities",
    "role_allows_operation",
    "require_project_access_service_principal",
    "normalize_access_assignment",
    "build_projection_fingerprint",
    "build_current_projection_fingerprint",
    "build_access_projection",
    "resolve_project_access_repository",
    "plan_project_access_reconciliation",
    "verify_project_access_projection",
    "sync_project_access_projection",
    "initialize_project_access",
    "transfer_project_owner",
    "authorize_project_operation",
    "require_project_operation",
    "get_project_role",
    "list_project_access_assignments",
    "get_project_access_status",
    "clear_project_access_cache",
    "serialize_access_assignment",
    "serialize_access_projection",
    "serialize_access_sync_plan",
    "serialize_access_sync_result",
    "serialize_access_decision",
    "serialize_owner_transfer_result",
    "get_project_access_service_status",
]
