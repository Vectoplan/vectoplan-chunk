# services/vectoplan-chunk/src/world_state/errors.py
"""
Structured errors for the VECTOPLAN world-state layer.

This module is framework-neutral and safe to use from:

- src/world_state/resolver.py
- src/world_state/service.py
- src/world_state/bootstrap.py
- src/world_state/serializer.py
- routes/*.py

It intentionally does not import Flask.

Error responsibility:

- `src.world.errors`
  Handles provider/template/generator/chunk-generation errors.

- `src.world_state.errors`
  Handles project/universe/world-instance resolution errors and productive
  project-scoped API state errors.

Important productive API distinction:

    worldId         = concrete project world instance, e.g. world_spawn
    templateId      = template/provider id, e.g. flat
    providerWorldId = provider world id used by src.world, e.g. flat
"""

from __future__ import annotations

import copy
import dataclasses
import json
import math
import traceback
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Mapping
from uuid import UUID


ERROR_SCHEMA_VERSION = "world-state-error.v1"
ERROR_MODULE_VERSION = "0.1.0"

DEFAULT_ERROR_CODE = "world_state_error"
DEFAULT_ERROR_MESSAGE = "A world-state error occurred."
DEFAULT_STATUS_CODE = 500

HTTP_STATUS_BAD_REQUEST = 400
HTTP_STATUS_NOT_FOUND = 404
HTTP_STATUS_CONFLICT = 409
HTTP_STATUS_UNPROCESSABLE_ENTITY = 422
HTTP_STATUS_INTERNAL_SERVER_ERROR = 500
HTTP_STATUS_SERVICE_UNAVAILABLE = 503

ERROR_CODE_STATUS_MAP: dict[str, int] = {
    "world_state_error": HTTP_STATUS_INTERNAL_SERVER_ERROR,
    "world_state_config_error": HTTP_STATUS_INTERNAL_SERVER_ERROR,
    "world_state_catalog_error": HTTP_STATUS_INTERNAL_SERVER_ERROR,
    "world_state_resolution_error": HTTP_STATUS_BAD_REQUEST,
    "world_state_bootstrap_error": HTTP_STATUS_INTERNAL_SERVER_ERROR,
    "world_state_serialization_error": HTTP_STATUS_INTERNAL_SERVER_ERROR,
    "world_state_provider_error": HTTP_STATUS_SERVICE_UNAVAILABLE,
    "invalid_world_state_model": HTTP_STATUS_BAD_REQUEST,
    "invalid_world_state_payload": HTTP_STATUS_BAD_REQUEST,
    "invalid_world_state_context": HTTP_STATUS_BAD_REQUEST,
    "invalid_project_id": HTTP_STATUS_BAD_REQUEST,
    "invalid_universe_id": HTTP_STATUS_BAD_REQUEST,
    "invalid_world_id": HTTP_STATUS_BAD_REQUEST,
    "invalid_template_id": HTTP_STATUS_BAD_REQUEST,
    "invalid_provider_world_id": HTTP_STATUS_BAD_REQUEST,
    "missing_project_id": HTTP_STATUS_BAD_REQUEST,
    "missing_universe_id": HTTP_STATUS_BAD_REQUEST,
    "missing_world_id": HTTP_STATUS_BAD_REQUEST,
    "missing_template_id": HTTP_STATUS_BAD_REQUEST,
    "missing_provider_world_id": HTTP_STATUS_BAD_REQUEST,
    "unknown_project": HTTP_STATUS_NOT_FOUND,
    "unknown_universe": HTTP_STATUS_NOT_FOUND,
    "unknown_world_instance": HTTP_STATUS_NOT_FOUND,
    "unknown_world_template": HTTP_STATUS_NOT_FOUND,
    "unknown_provider_world": HTTP_STATUS_NOT_FOUND,
    "invalid_project_universe_binding": HTTP_STATUS_CONFLICT,
    "invalid_project_world_binding": HTTP_STATUS_CONFLICT,
    "invalid_universe_world_binding": HTTP_STATUS_CONFLICT,
    "world_not_in_project": HTTP_STATUS_CONFLICT,
    "world_not_in_universe": HTTP_STATUS_CONFLICT,
    "template_provider_mismatch": HTTP_STATUS_CONFLICT,
    "provider_world_resolution_failed": HTTP_STATUS_SERVICE_UNAVAILABLE,
}

PUBLIC_ERROR_MESSAGES: dict[str, str] = {
    "world_state_error": "A world-state error occurred.",
    "world_state_config_error": "The world-state configuration is invalid.",
    "world_state_catalog_error": "The world-state catalog is invalid.",
    "world_state_resolution_error": "The requested world-state context could not be resolved.",
    "world_state_bootstrap_error": "The project bootstrap context could not be created.",
    "world_state_serialization_error": "The world-state response could not be serialized.",
    "world_state_provider_error": "The backing world provider could not be used.",
    "invalid_world_state_model": "The world-state model is invalid.",
    "invalid_world_state_payload": "The world-state request payload is invalid.",
    "invalid_world_state_context": "The world-state context is invalid.",
    "invalid_project_id": "The project id is invalid.",
    "invalid_universe_id": "The universe id is invalid.",
    "invalid_world_id": "The world id is invalid.",
    "invalid_template_id": "The template id is invalid.",
    "invalid_provider_world_id": "The provider world id is invalid.",
    "missing_project_id": "The project id is required.",
    "missing_universe_id": "The universe id is required.",
    "missing_world_id": "The world id is required.",
    "missing_template_id": "The template id is required.",
    "missing_provider_world_id": "The provider world id is required.",
    "unknown_project": "The requested project does not exist.",
    "unknown_universe": "The requested universe does not exist.",
    "unknown_world_instance": "The requested world does not exist in this project.",
    "unknown_world_template": "The requested world template does not exist.",
    "unknown_provider_world": "The requested provider world does not exist.",
    "invalid_project_universe_binding": "The universe does not belong to the project.",
    "invalid_project_world_binding": "The world does not belong to the project.",
    "invalid_universe_world_binding": "The world does not belong to the universe.",
    "world_not_in_project": "The world does not belong to the project.",
    "world_not_in_universe": "The world does not belong to the universe.",
    "template_provider_mismatch": "The world template and provider world do not match.",
    "provider_world_resolution_failed": "The provider world could not be resolved.",
}

_INTERNAL_DETAIL_KEYS = {
    "traceback",
    "stack",
    "stackTrace",
    "exception",
    "exc",
    "rawException",
    "rawTraceback",
}


def make_json_safe(value: Any, *, include_private: bool = False) -> Any:
    """
    Convert arbitrary values into JSON-safe values.

    This function is deliberately duplicated here instead of importing from
    models.py. Error handling must remain robust even when model imports fail.
    """
    if value is None:
        return None

    if isinstance(value, (str, int, bool)):
        return value

    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            return None
        return value

    if isinstance(value, Decimal):
        return float(value)

    if isinstance(value, (datetime, date)):
        try:
            return value.isoformat()
        except Exception:
            return str(value)

    if isinstance(value, UUID):
        return str(value)

    if isinstance(value, Path):
        return str(value)

    if dataclasses.is_dataclass(value):
        try:
            if hasattr(value, "to_dict") and callable(value.to_dict):
                return make_json_safe(value.to_dict(), include_private=include_private)
        except Exception:
            pass

        try:
            return make_json_safe(dataclasses.asdict(value), include_private=include_private)
        except Exception:
            return str(value)

    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for key, item in value.items():
            try:
                safe_key = str(key)
            except Exception:
                safe_key = repr(key)

            if not include_private and safe_key in _INTERNAL_DETAIL_KEYS:
                continue

            result[safe_key] = make_json_safe(item, include_private=include_private)

        return result

    if isinstance(value, (list, tuple, set, frozenset)):
        return [
            make_json_safe(item, include_private=include_private)
            for item in value
        ]

    try:
        json.dumps(value)
        return value
    except Exception:
        return str(value)


def normalize_error_code(value: Any, *, fallback: str = DEFAULT_ERROR_CODE) -> str:
    """
    Normalize an error code for API responses.
    """
    try:
        code = str(value or fallback).strip()
    except Exception:
        code = fallback

    return code or fallback


def normalize_error_message(value: Any, *, fallback: str = DEFAULT_ERROR_MESSAGE) -> str:
    """
    Normalize an error message for API responses.
    """
    try:
        message = str(value or fallback).strip()
    except Exception:
        message = fallback

    return message or fallback


def normalize_status_code(value: Any, *, fallback: int = DEFAULT_STATUS_CODE) -> int:
    """
    Normalize a HTTP status code.
    """
    try:
        status_code = int(value)
    except Exception:
        return int(fallback)

    if status_code < 100 or status_code > 599:
        return int(fallback)

    return status_code


def normalize_error_details(
    details: Mapping[str, Any] | None = None,
    *,
    include_private: bool = False,
) -> dict[str, Any]:
    """
    Normalize details into a JSON-safe dictionary.
    """
    if details is None:
        return {}

    safe = make_json_safe(details, include_private=include_private)

    if isinstance(safe, Mapping):
        return dict(safe)

    return {"value": safe}


def _safe_exception_message(exc: BaseException) -> str:
    try:
        message = str(exc)
    except Exception:
        message = exc.__class__.__name__

    return message or exc.__class__.__name__


def get_default_public_message(code: str) -> str:
    """
    Return the public message for an error code.
    """
    normalized_code = normalize_error_code(code)

    return PUBLIC_ERROR_MESSAGES.get(
        normalized_code,
        PUBLIC_ERROR_MESSAGES.get(DEFAULT_ERROR_CODE, DEFAULT_ERROR_MESSAGE),
    )


def get_error_status_code(error_or_code: Any) -> int:
    """
    Resolve the HTTP status code for an error object or error code.
    """
    if isinstance(error_or_code, WorldStateError):
        return normalize_status_code(error_or_code.status_code)

    code = normalize_error_code(
        getattr(error_or_code, "code", error_or_code),
        fallback=DEFAULT_ERROR_CODE,
    )

    return ERROR_CODE_STATUS_MAP.get(code, DEFAULT_STATUS_CODE)


class WorldStateError(Exception):
    """
    Base class for all world-state errors.

    Args:
        message:
            Internal developer-facing message.
        code:
            Stable machine-readable error code.
        status_code:
            HTTP status code that routes can use.
        details:
            JSON-safe context. Internal keys are filtered from public API
            responses unless explicitly requested.
        public_message:
            Optional user-facing message. If omitted, a stable message is
            derived from the error code.
        cause:
            Optional original exception.
    """

    code = DEFAULT_ERROR_CODE
    status_code = DEFAULT_STATUS_CODE
    public_message: str | None = None

    def __init__(
        self,
        message: str | None = None,
        *,
        code: str | None = None,
        status_code: int | None = None,
        details: Mapping[str, Any] | None = None,
        public_message: str | None = None,
        cause: BaseException | None = None,
    ) -> None:
        resolved_code = normalize_error_code(code or self.code)
        resolved_message = normalize_error_message(
            message,
            fallback=get_default_public_message(resolved_code),
        )
        resolved_status_code = normalize_status_code(
            status_code if status_code is not None else self.status_code,
            fallback=ERROR_CODE_STATUS_MAP.get(resolved_code, DEFAULT_STATUS_CODE),
        )

        super().__init__(resolved_message)

        self.code = resolved_code
        self.status_code = resolved_status_code
        self.details = normalize_error_details(details)
        self.public_message = normalize_error_message(
            public_message or self.public_message or get_default_public_message(resolved_code),
            fallback=DEFAULT_ERROR_MESSAGE,
        )
        self.cause = cause

    def to_dict(
        self,
        *,
        include_private: bool = False,
        include_debug: bool = False,
    ) -> dict[str, Any]:
        """
        Convert the error to an API-safe dictionary.

        By default, only public-safe details are included.
        """
        details = normalize_error_details(
            self.details,
            include_private=include_private,
        )

        if include_debug:
            details = copy.deepcopy(details)
            details["debug"] = {
                "exceptionType": self.__class__.__name__,
                "internalMessage": _safe_exception_message(self),
                "causeType": self.cause.__class__.__name__ if self.cause else None,
                "causeMessage": _safe_exception_message(self.cause) if self.cause else None,
            }

        return {
            "schemaVersion": ERROR_SCHEMA_VERSION,
            "code": self.code,
            "message": self.public_message,
            "details": details,
        }

    def to_log_dict(self, *, include_traceback: bool = False) -> dict[str, Any]:
        """
        Convert the error to a log-oriented dictionary.
        """
        payload: dict[str, Any] = {
            "schemaVersion": ERROR_SCHEMA_VERSION,
            "type": self.__class__.__name__,
            "code": self.code,
            "statusCode": self.status_code,
            "message": _safe_exception_message(self),
            "publicMessage": self.public_message,
            "details": normalize_error_details(self.details, include_private=True),
            "cause": None,
        }

        if self.cause is not None:
            payload["cause"] = {
                "type": self.cause.__class__.__name__,
                "message": _safe_exception_message(self.cause),
            }

        if include_traceback:
            payload["traceback"] = traceback.format_exc()

        return make_json_safe(payload, include_private=True)

    def with_details(self, **details: Any) -> "WorldStateError":
        """
        Return a new error object with merged details.
        """
        merged = normalize_error_details(self.details, include_private=True)
        merged.update(normalize_error_details(details, include_private=True))

        return self.__class__(
            _safe_exception_message(self),
            code=self.code,
            status_code=self.status_code,
            details=merged,
            public_message=self.public_message,
            cause=self.cause,
        )


class WorldStateConfigError(WorldStateError):
    code = "world_state_config_error"
    status_code = HTTP_STATUS_INTERNAL_SERVER_ERROR


class WorldStateCatalogError(WorldStateError):
    code = "world_state_catalog_error"
    status_code = HTTP_STATUS_INTERNAL_SERVER_ERROR


class WorldStateResolutionError(WorldStateError):
    code = "world_state_resolution_error"
    status_code = HTTP_STATUS_BAD_REQUEST


class WorldStateBootstrapError(WorldStateError):
    code = "world_state_bootstrap_error"
    status_code = HTTP_STATUS_INTERNAL_SERVER_ERROR


class WorldStateSerializationError(WorldStateError):
    code = "world_state_serialization_error"
    status_code = HTTP_STATUS_INTERNAL_SERVER_ERROR


class WorldStateProviderError(WorldStateError):
    code = "world_state_provider_error"
    status_code = HTTP_STATUS_SERVICE_UNAVAILABLE


class InvalidWorldStatePayloadError(WorldStateError):
    code = "invalid_world_state_payload"
    status_code = HTTP_STATUS_BAD_REQUEST


class InvalidWorldStateContextError(WorldStateError):
    code = "invalid_world_state_context"
    status_code = HTTP_STATUS_BAD_REQUEST


class ProjectNotFoundError(WorldStateResolutionError):
    code = "unknown_project"
    status_code = HTTP_STATUS_NOT_FOUND

    def __init__(
        self,
        project_id: Any,
        *,
        details: Mapping[str, Any] | None = None,
        cause: BaseException | None = None,
    ) -> None:
        normalized_details = normalize_error_details(details)
        normalized_details.setdefault("projectId", make_json_safe(project_id))

        super().__init__(
            "Project could not be resolved.",
            code=self.code,
            status_code=self.status_code,
            details=normalized_details,
            public_message=get_default_public_message(self.code),
            cause=cause,
        )


class UniverseNotFoundError(WorldStateResolutionError):
    code = "unknown_universe"
    status_code = HTTP_STATUS_NOT_FOUND

    def __init__(
        self,
        universe_id: Any,
        *,
        project_id: Any | None = None,
        details: Mapping[str, Any] | None = None,
        cause: BaseException | None = None,
    ) -> None:
        normalized_details = normalize_error_details(details)
        normalized_details.setdefault("universeId", make_json_safe(universe_id))

        if project_id is not None:
            normalized_details.setdefault("projectId", make_json_safe(project_id))

        super().__init__(
            "Universe could not be resolved.",
            code=self.code,
            status_code=self.status_code,
            details=normalized_details,
            public_message=get_default_public_message(self.code),
            cause=cause,
        )


class WorldInstanceNotFoundError(WorldStateResolutionError):
    code = "unknown_world_instance"
    status_code = HTTP_STATUS_NOT_FOUND

    def __init__(
        self,
        world_id: Any,
        *,
        project_id: Any | None = None,
        universe_id: Any | None = None,
        details: Mapping[str, Any] | None = None,
        cause: BaseException | None = None,
    ) -> None:
        normalized_details = normalize_error_details(details)
        normalized_details.setdefault("worldId", make_json_safe(world_id))

        if project_id is not None:
            normalized_details.setdefault("projectId", make_json_safe(project_id))

        if universe_id is not None:
            normalized_details.setdefault("universeId", make_json_safe(universe_id))

        super().__init__(
            "World instance could not be resolved.",
            code=self.code,
            status_code=self.status_code,
            details=normalized_details,
            public_message=get_default_public_message(self.code),
            cause=cause,
        )


class InvalidProjectUniverseBindingError(WorldStateResolutionError):
    code = "invalid_project_universe_binding"
    status_code = HTTP_STATUS_CONFLICT

    def __init__(
        self,
        *,
        project_id: Any,
        universe_id: Any,
        universe_project_id: Any | None = None,
        details: Mapping[str, Any] | None = None,
        cause: BaseException | None = None,
    ) -> None:
        normalized_details = normalize_error_details(details)
        normalized_details.setdefault("projectId", make_json_safe(project_id))
        normalized_details.setdefault("universeId", make_json_safe(universe_id))

        if universe_project_id is not None:
            normalized_details.setdefault(
                "universeProjectId",
                make_json_safe(universe_project_id),
            )

        super().__init__(
            "Universe does not belong to project.",
            code=self.code,
            status_code=self.status_code,
            details=normalized_details,
            public_message=get_default_public_message(self.code),
            cause=cause,
        )


class InvalidProjectWorldBindingError(WorldStateResolutionError):
    code = "invalid_project_world_binding"
    status_code = HTTP_STATUS_CONFLICT

    def __init__(
        self,
        *,
        project_id: Any,
        world_id: Any,
        world_project_id: Any | None = None,
        details: Mapping[str, Any] | None = None,
        cause: BaseException | None = None,
    ) -> None:
        normalized_details = normalize_error_details(details)
        normalized_details.setdefault("projectId", make_json_safe(project_id))
        normalized_details.setdefault("worldId", make_json_safe(world_id))

        if world_project_id is not None:
            normalized_details.setdefault(
                "worldProjectId",
                make_json_safe(world_project_id),
            )

        super().__init__(
            "World does not belong to project.",
            code=self.code,
            status_code=self.status_code,
            details=normalized_details,
            public_message=get_default_public_message(self.code),
            cause=cause,
        )


class InvalidUniverseWorldBindingError(WorldStateResolutionError):
    code = "invalid_universe_world_binding"
    status_code = HTTP_STATUS_CONFLICT

    def __init__(
        self,
        *,
        universe_id: Any,
        world_id: Any,
        world_universe_id: Any | None = None,
        project_id: Any | None = None,
        details: Mapping[str, Any] | None = None,
        cause: BaseException | None = None,
    ) -> None:
        normalized_details = normalize_error_details(details)
        normalized_details.setdefault("universeId", make_json_safe(universe_id))
        normalized_details.setdefault("worldId", make_json_safe(world_id))

        if world_universe_id is not None:
            normalized_details.setdefault(
                "worldUniverseId",
                make_json_safe(world_universe_id),
            )

        if project_id is not None:
            normalized_details.setdefault("projectId", make_json_safe(project_id))

        super().__init__(
            "World does not belong to universe.",
            code=self.code,
            status_code=self.status_code,
            details=normalized_details,
            public_message=get_default_public_message(self.code),
            cause=cause,
        )


class WorldTemplateNotFoundError(WorldStateResolutionError):
    code = "unknown_world_template"
    status_code = HTTP_STATUS_NOT_FOUND

    def __init__(
        self,
        template_id: Any,
        *,
        project_id: Any | None = None,
        universe_id: Any | None = None,
        world_id: Any | None = None,
        provider_world_id: Any | None = None,
        details: Mapping[str, Any] | None = None,
        cause: BaseException | None = None,
    ) -> None:
        normalized_details = normalize_error_details(details)
        normalized_details.setdefault("templateId", make_json_safe(template_id))

        if project_id is not None:
            normalized_details.setdefault("projectId", make_json_safe(project_id))

        if universe_id is not None:
            normalized_details.setdefault("universeId", make_json_safe(universe_id))

        if world_id is not None:
            normalized_details.setdefault("worldId", make_json_safe(world_id))

        if provider_world_id is not None:
            normalized_details.setdefault(
                "providerWorldId",
                make_json_safe(provider_world_id),
            )

        super().__init__(
            "World template could not be resolved.",
            code=self.code,
            status_code=self.status_code,
            details=normalized_details,
            public_message=get_default_public_message(self.code),
            cause=cause,
        )


class ProviderWorldNotFoundError(WorldStateResolutionError):
    code = "unknown_provider_world"
    status_code = HTTP_STATUS_NOT_FOUND

    def __init__(
        self,
        provider_world_id: Any,
        *,
        template_id: Any | None = None,
        project_id: Any | None = None,
        universe_id: Any | None = None,
        world_id: Any | None = None,
        details: Mapping[str, Any] | None = None,
        cause: BaseException | None = None,
    ) -> None:
        normalized_details = normalize_error_details(details)
        normalized_details.setdefault(
            "providerWorldId",
            make_json_safe(provider_world_id),
        )

        if template_id is not None:
            normalized_details.setdefault("templateId", make_json_safe(template_id))

        if project_id is not None:
            normalized_details.setdefault("projectId", make_json_safe(project_id))

        if universe_id is not None:
            normalized_details.setdefault("universeId", make_json_safe(universe_id))

        if world_id is not None:
            normalized_details.setdefault("worldId", make_json_safe(world_id))

        super().__init__(
            "Provider world could not be resolved.",
            code=self.code,
            status_code=self.status_code,
            details=normalized_details,
            public_message=get_default_public_message(self.code),
            cause=cause,
        )


class ProviderWorldResolutionError(WorldStateProviderError):
    code = "provider_world_resolution_failed"
    status_code = HTTP_STATUS_SERVICE_UNAVAILABLE

    def __init__(
        self,
        provider_world_id: Any,
        *,
        template_id: Any | None = None,
        project_id: Any | None = None,
        universe_id: Any | None = None,
        world_id: Any | None = None,
        details: Mapping[str, Any] | None = None,
        cause: BaseException | None = None,
    ) -> None:
        normalized_details = normalize_error_details(details)
        normalized_details.setdefault(
            "providerWorldId",
            make_json_safe(provider_world_id),
        )

        if template_id is not None:
            normalized_details.setdefault("templateId", make_json_safe(template_id))

        if project_id is not None:
            normalized_details.setdefault("projectId", make_json_safe(project_id))

        if universe_id is not None:
            normalized_details.setdefault("universeId", make_json_safe(universe_id))

        if world_id is not None:
            normalized_details.setdefault("worldId", make_json_safe(world_id))

        super().__init__(
            "Provider world resolution failed.",
            code=self.code,
            status_code=self.status_code,
            details=normalized_details,
            public_message=get_default_public_message(self.code),
            cause=cause,
        )


def coerce_world_state_error(
    error: BaseException | Any,
    *,
    default_code: str = DEFAULT_ERROR_CODE,
    default_status_code: int | None = None,
    details: Mapping[str, Any] | None = None,
    include_original_details: bool = True,
) -> WorldStateError:
    """
    Convert any exception-like value into a WorldStateError.

    This allows routes and service code to handle:
    - dedicated WorldStateError subclasses
    - ModelValidationError from models.py
    - ValueError/KeyError/etc.
    - provider/generator exceptions from src.world
    """
    if isinstance(error, WorldStateError):
        if not details:
            return error

        merged_details = normalize_error_details(error.details, include_private=True)
        merged_details.update(normalize_error_details(details, include_private=True))

        return error.__class__(
            _safe_exception_message(error),
            code=error.code,
            status_code=error.status_code,
            details=merged_details,
            public_message=error.public_message,
            cause=error.cause,
        )

    original_code = normalize_error_code(
        getattr(error, "code", default_code),
        fallback=default_code,
    )

    status_code = normalize_status_code(
        getattr(
            error,
            "status_code",
            getattr(error, "statusCode", default_status_code),
        )
        if default_status_code is not None or hasattr(error, "status_code") or hasattr(error, "statusCode")
        else ERROR_CODE_STATUS_MAP.get(original_code, DEFAULT_STATUS_CODE),
        fallback=ERROR_CODE_STATUS_MAP.get(original_code, DEFAULT_STATUS_CODE),
    )

    base_details = normalize_error_details(details, include_private=True)

    if include_original_details:
        original_details = getattr(error, "details", None)
        if isinstance(original_details, Mapping):
            base_details.update(
                normalize_error_details(original_details, include_private=True)
            )

    if not isinstance(error, BaseException):
        message = normalize_error_message(error)
        cause = None
        base_details.setdefault("originalType", type(error).__name__)
        base_details.setdefault("originalValue", make_json_safe(error))
    else:
        message = _safe_exception_message(error)
        cause = error
        base_details.setdefault("originalType", error.__class__.__name__)

    # Preserve known model validation codes without importing models.py.
    if original_code == "invalid_world_state_model":
        return InvalidWorldStatePayloadError(
            message,
            code=original_code,
            status_code=status_code,
            details=base_details,
            public_message=get_default_public_message(original_code),
            cause=cause,
        )

    error_class: type[WorldStateError]

    if original_code in {
        "unknown_project",
    }:
        return WorldStateError(
            message,
            code=original_code,
            status_code=ERROR_CODE_STATUS_MAP.get(original_code, HTTP_STATUS_NOT_FOUND),
            details=base_details,
            public_message=get_default_public_message(original_code),
            cause=cause,
        )

    if original_code.startswith("unknown_"):
        error_class = WorldStateResolutionError
    elif original_code.startswith("invalid_") or original_code.startswith("missing_"):
        error_class = InvalidWorldStatePayloadError
    elif "provider" in original_code:
        error_class = WorldStateProviderError
    elif "serialization" in original_code:
        error_class = WorldStateSerializationError
    elif "config" in original_code:
        error_class = WorldStateConfigError
    elif "catalog" in original_code:
        error_class = WorldStateCatalogError
    else:
        error_class = WorldStateError

    return error_class(
        message,
        code=original_code,
        status_code=status_code,
        details=base_details,
        public_message=get_default_public_message(original_code),
        cause=cause,
    )


def error_to_api_response_body(
    error: BaseException | Any,
    *,
    include_debug: bool = False,
    include_private: bool = False,
) -> dict[str, Any]:
    """
    Convert an error into the standard API response body.

    Routes can use this function and pair it with `get_error_status_code()`.
    """
    world_state_error = coerce_world_state_error(error)

    return {
        "ok": False,
        "error": world_state_error.to_dict(
            include_private=include_private,
            include_debug=include_debug,
        ),
    }


def error_to_log_dict(
    error: BaseException | Any,
    *,
    include_traceback: bool = False,
) -> dict[str, Any]:
    """
    Convert an error into a log-safe dictionary.
    """
    world_state_error = coerce_world_state_error(error)

    return world_state_error.to_log_dict(
        include_traceback=include_traceback,
    )


def error_to_response_tuple(
    error: BaseException | Any,
    *,
    include_debug: bool = False,
    include_private: bool = False,
) -> tuple[dict[str, Any], int]:
    """
    Convert an error into `(body, status_code)`.

    This remains framework-neutral. Flask routes can do:

        body, status = error_to_response_tuple(exc)
        return jsonify(body), status
    """
    world_state_error = coerce_world_state_error(error)

    body = {
        "ok": False,
        "error": world_state_error.to_dict(
            include_private=include_private,
            include_debug=include_debug,
        ),
    }

    return body, get_error_status_code(world_state_error)


def raise_for_missing_project_id(project_id: Any) -> None:
    if project_id is None or str(project_id).strip() == "":
        raise InvalidWorldStatePayloadError(
            "Project id is required.",
            code="missing_project_id",
            status_code=HTTP_STATUS_BAD_REQUEST,
            details={"projectId": project_id},
            public_message=get_default_public_message("missing_project_id"),
        )


def raise_for_missing_universe_id(universe_id: Any) -> None:
    if universe_id is None or str(universe_id).strip() == "":
        raise InvalidWorldStatePayloadError(
            "Universe id is required.",
            code="missing_universe_id",
            status_code=HTTP_STATUS_BAD_REQUEST,
            details={"universeId": universe_id},
            public_message=get_default_public_message("missing_universe_id"),
        )


def raise_for_missing_world_id(world_id: Any) -> None:
    if world_id is None or str(world_id).strip() == "":
        raise InvalidWorldStatePayloadError(
            "World id is required.",
            code="missing_world_id",
            status_code=HTTP_STATUS_BAD_REQUEST,
            details={"worldId": world_id},
            public_message=get_default_public_message("missing_world_id"),
        )


def raise_for_missing_template_id(template_id: Any) -> None:
    if template_id is None or str(template_id).strip() == "":
        raise InvalidWorldStatePayloadError(
            "Template id is required.",
            code="missing_template_id",
            status_code=HTTP_STATUS_BAD_REQUEST,
            details={"templateId": template_id},
            public_message=get_default_public_message("missing_template_id"),
        )


def raise_for_missing_provider_world_id(provider_world_id: Any) -> None:
    if provider_world_id is None or str(provider_world_id).strip() == "":
        raise InvalidWorldStatePayloadError(
            "Provider world id is required.",
            code="missing_provider_world_id",
            status_code=HTTP_STATUS_BAD_REQUEST,
            details={"providerWorldId": provider_world_id},
            public_message=get_default_public_message("missing_provider_world_id"),
        )


def build_error_diagnostics() -> dict[str, Any]:
    """
    Return module diagnostics for health/debug endpoints.
    """
    return {
        "ok": True,
        "module": "src.world_state.errors",
        "moduleVersion": ERROR_MODULE_VERSION,
        "schemaVersion": ERROR_SCHEMA_VERSION,
        "errorCodeCount": len(ERROR_CODE_STATUS_MAP),
        "publicMessageCount": len(PUBLIC_ERROR_MESSAGES),
        "knownCodes": sorted(ERROR_CODE_STATUS_MAP.keys()),
    }


__all__ = (
    "ERROR_SCHEMA_VERSION",
    "ERROR_MODULE_VERSION",
    "DEFAULT_ERROR_CODE",
    "DEFAULT_ERROR_MESSAGE",
    "DEFAULT_STATUS_CODE",
    "HTTP_STATUS_BAD_REQUEST",
    "HTTP_STATUS_NOT_FOUND",
    "HTTP_STATUS_CONFLICT",
    "HTTP_STATUS_UNPROCESSABLE_ENTITY",
    "HTTP_STATUS_INTERNAL_SERVER_ERROR",
    "HTTP_STATUS_SERVICE_UNAVAILABLE",
    "ERROR_CODE_STATUS_MAP",
    "PUBLIC_ERROR_MESSAGES",
    "make_json_safe",
    "normalize_error_code",
    "normalize_error_message",
    "normalize_status_code",
    "normalize_error_details",
    "get_default_public_message",
    "get_error_status_code",
    "WorldStateError",
    "WorldStateConfigError",
    "WorldStateCatalogError",
    "WorldStateResolutionError",
    "WorldStateBootstrapError",
    "WorldStateSerializationError",
    "WorldStateProviderError",
    "InvalidWorldStatePayloadError",
    "InvalidWorldStateContextError",
    "ProjectNotFoundError",
    "UniverseNotFoundError",
    "WorldInstanceNotFoundError",
    "InvalidProjectUniverseBindingError",
    "InvalidProjectWorldBindingError",
    "InvalidUniverseWorldBindingError",
    "WorldTemplateNotFoundError",
    "ProviderWorldNotFoundError",
    "ProviderWorldResolutionError",
    "coerce_world_state_error",
    "error_to_api_response_body",
    "error_to_log_dict",
    "error_to_response_tuple",
    "raise_for_missing_project_id",
    "raise_for_missing_universe_id",
    "raise_for_missing_world_id",
    "raise_for_missing_template_id",
    "raise_for_missing_provider_world_id",
    "build_error_diagnostics",
)