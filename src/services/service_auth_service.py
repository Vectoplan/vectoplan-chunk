# services/vectoplan-chunk/src/services/service_auth_service.py
"""
Service-to-service authentication for ``vectoplan-chunk``.

This module implements the inbound internal-service authentication contract
configured in :mod:`config`.  It is deliberately self-contained and performs
no database access, no outbound HTTP calls, and no project authorization.

Responsibilities
----------------

* authenticate trusted calling services with a shared internal credential,
* validate the service identifier against the configured allow-list,
* parse request, correlation, and idempotency identifiers defensively,
* support exact health/status-route exemptions,
* expose a compact immutable service principal to downstream guards,
* integrate with Flask without requiring Flask at import time,
* return redacted diagnostics and errors that never expose raw credentials.

Non-responsibilities
--------------------

* It does not authenticate end users.
* It does not accept or resolve ``auth_user_id`` values.
* It does not decide project membership or project role.
* It does not perform Chunk project access authorization.
* It does not create sessions, cookies, projects, worlds, or assignments.

The caller identity from this module is the *calling service*.  Canonical user
identity and App-owned project-role projection are handled by the dedicated
access-control layer.
"""

from __future__ import annotations

import functools
import hashlib
import hmac
import json
import logging
import re
import secrets
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Mapping, MutableMapping, Optional, Sequence
from urllib.parse import urlsplit


LOGGER = logging.getLogger(__name__)

SERVICE_AUTH_VERSION = "1.0.0"
SERVICE_PRINCIPAL_SCHEMA_VERSION = "vectoplan-service-principal-v1"
SERVICE_AUTH_RESULT_SCHEMA_VERSION = "vectoplan-service-auth-result-v1"
REQUEST_CONTEXT_SCHEMA_VERSION = "vectoplan-request-correlation-v1"

DEFAULT_SERVICE_ID_HEADERS: tuple[str, ...] = (
    "X-VECTOPLAN-Service-ID",
    "X-VECTOPLAN-Service",
)
DEFAULT_CREDENTIAL_HEADERS: tuple[str, ...] = (
    "Authorization",
    "X-API-Key",
    "X-Vectoplan-Internal-Token",
)
DEFAULT_EXEMPT_PATHS: tuple[str, ...] = (
    "/",
    "/health",
    "/health/live",
    "/health/ready",
    "/projects/_status",
    "/chunks/_status",
    "/commands/_status",
)
DEFAULT_ALLOWED_SERVICE_IDS: tuple[str, ...] = (
    "vectoplan-app",
    "vectoplan-editor",
    "vectoplan-chunk-init",
)

DEFAULT_SERVICE_ID_HEADER = "X-VECTOPLAN-Service-ID"
DEFAULT_CREDENTIAL_HEADER = "X-API-Key"
DEFAULT_REQUEST_ID_HEADER = "X-Request-ID"
DEFAULT_CORRELATION_ID_HEADER = "X-Correlation-ID"
DEFAULT_IDEMPOTENCY_KEY_HEADER = "Idempotency-Key"
DEFAULT_MAX_TOKEN_LENGTH = 4096
DEFAULT_MAX_REQUEST_ID_LENGTH = 160
DEFAULT_MAX_IDEMPOTENCY_KEY_LENGTH = 255
DEFAULT_MAX_SERVICE_ID_LENGTH = 120

AUTH_CODE_OK = "service_authenticated"
AUTH_CODE_EXEMPT = "service_auth_exempt"
AUTH_CODE_DISABLED = "service_auth_disabled"
AUTH_CODE_MISSING_SERVICE_ID = "service_id_missing"
AUTH_CODE_CONFLICTING_SERVICE_ID = "service_id_conflict"
AUTH_CODE_INVALID_SERVICE_ID = "service_id_invalid"
AUTH_CODE_SERVICE_NOT_ALLOWED = "service_not_allowed"
AUTH_CODE_MISSING_CREDENTIAL = "service_credential_missing"
AUTH_CODE_CONFLICTING_CREDENTIAL = "service_credential_conflict"
AUTH_CODE_INVALID_CREDENTIAL = "service_credential_invalid"
AUTH_CODE_CREDENTIAL_TOO_LONG = "service_credential_too_long"
AUTH_CODE_UNSUPPORTED_SCHEME = "service_credential_scheme_unsupported"
AUTH_CODE_AUTH_NOT_CONFIGURED = "service_auth_not_configured"
AUTH_CODE_REQUEST_INVALID = "service_auth_request_invalid"
AUTH_CODE_INTERNAL_ERROR = "service_auth_internal_error"

_HEADER_NAME_RE = re.compile(r"^[A-Za-z0-9-]{1,128}$")
_SERVICE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._:-]{0,119}$")
_SAFE_IDENTIFIER_RE = re.compile(r"[^A-Za-z0-9._:-]+")
_CONTROL_CHAR_RE = re.compile(r"[\x00-\x1f\x7f]")
_WHITESPACE_RE = re.compile(r"\s+")
_MULTI_SEPARATOR_RE = re.compile(r"[-_.:]{3,}")

_SECRET_KEY_FRAGMENTS: tuple[str, ...] = (
    "authorization",
    "api_key",
    "apikey",
    "access_token",
    "bearer",
    "cookie",
    "credential",
    "internal_token",
    "password",
    "private_key",
    "secret",
    "session",
    "token",
)

_IDENTITY_KEY_FRAGMENTS: tuple[str, ...] = (
    "auth_user_id",
    "account_id",
    "actor_user_id",
    "owner_user_id",
    "local_user_id",
    "email",
)


class ServiceAuthenticationError(RuntimeError):
    """Structured authentication error safe for API translation."""

    def __init__(
        self,
        message: str,
        *,
        code: str = AUTH_CODE_INTERNAL_ERROR,
        status_code: int = 401,
        retryable: bool = False,
        details: Optional[Mapping[str, Any]] = None,
        request_id: str = "",
        correlation_id: str = "",
    ) -> None:
        super().__init__(str(message or "Service authentication failed."))
        self.code = _safe_code(code, AUTH_CODE_INTERNAL_ERROR)
        self.status_code = _bounded_int(status_code, 401, 400, 599)
        self.retryable = bool(retryable)
        self.details = sanitize_diagnostic_mapping(details or {})
        self.request_id = _normalize_request_identifier(
            request_id,
            prefix="req",
            maximum=DEFAULT_MAX_REQUEST_ID_LENGTH,
            generate=False,
        )
        self.correlation_id = _normalize_request_identifier(
            correlation_id,
            prefix="corr",
            maximum=DEFAULT_MAX_REQUEST_ID_LENGTH,
            generate=False,
        )

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "ok": False,
            "code": self.code,
            "error": str(self),
            "status_code": self.status_code,
            "retryable": self.retryable,
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
class RequestCorrelationContext:
    """Normalized request identifiers without raw credentials."""

    request_id: str
    correlation_id: str
    idempotency_key: str = field(default="", repr=False)
    idempotency_key_hash: str = ""
    request_id_source: str = "generated"
    correlation_id_source: str = "request_id"
    idempotency_key_present: bool = False
    schema_version: str = REQUEST_CONTEXT_SCHEMA_VERSION

    def to_dict(self, *, include_idempotency_key: bool = False) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "schema_version": self.schema_version,
            "schemaVersion": self.schema_version,
            "request_id": self.request_id,
            "requestId": self.request_id,
            "correlation_id": self.correlation_id,
            "correlationId": self.correlation_id,
            "request_id_source": self.request_id_source,
            "requestIdSource": self.request_id_source,
            "correlation_id_source": self.correlation_id_source,
            "correlationIdSource": self.correlation_id_source,
            "idempotency_key_present": self.idempotency_key_present,
            "idempotencyKeyPresent": self.idempotency_key_present,
            "idempotency_key_hash": self.idempotency_key_hash or None,
            "idempotencyKeyHash": self.idempotency_key_hash or None,
        }
        if include_idempotency_key and self.idempotency_key:
            payload["idempotency_key"] = self.idempotency_key
            payload["idempotencyKey"] = self.idempotency_key
        return payload


@dataclass(frozen=True, slots=True)
class ServicePrincipal:
    """Immutable authenticated calling-service principal."""

    service_id: str
    authenticated: bool
    auth_required: bool
    exempt: bool
    credential_scheme: str
    credential_header: str
    credential_fingerprint: str
    request_id: str
    correlation_id: str
    idempotency_key_hash: str
    auth_source: str = "shared_service_key"
    issued_at_unix: float = field(default_factory=time.time)
    schema_version: str = SERVICE_PRINCIPAL_SCHEMA_VERSION

    @property
    def is_trusted_service(self) -> bool:
        return bool(self.authenticated and self.service_id and not self.exempt)

    @property
    def is_public_exempt_request(self) -> bool:
        return bool(self.exempt and not self.authenticated)

    def to_dict(self, *, include_private: bool = False) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "schema_version": self.schema_version,
            "schemaVersion": self.schema_version,
            "service_id": self.service_id or None,
            "serviceId": self.service_id or None,
            "authenticated": self.authenticated,
            "trusted_service": self.is_trusted_service,
            "trustedService": self.is_trusted_service,
            "auth_required": self.auth_required,
            "authRequired": self.auth_required,
            "exempt": self.exempt,
            "credential_scheme": self.credential_scheme or None,
            "credentialScheme": self.credential_scheme or None,
            "auth_source": self.auth_source,
            "authSource": self.auth_source,
            "request_id": self.request_id,
            "requestId": self.request_id,
            "correlation_id": self.correlation_id,
            "correlationId": self.correlation_id,
            "idempotency_key_hash": self.idempotency_key_hash or None,
            "idempotencyKeyHash": self.idempotency_key_hash or None,
        }
        if include_private:
            payload["credential_header"] = self.credential_header or None
            payload["credentialHeader"] = self.credential_header or None
            payload["credential_fingerprint"] = self.credential_fingerprint or None
            payload["credentialFingerprint"] = self.credential_fingerprint or None
            payload["issued_at_unix"] = self.issued_at_unix
            payload["issuedAtUnix"] = self.issued_at_unix
        return payload


@dataclass(frozen=True, slots=True)
class ServiceAuthResult:
    """Complete result of one inbound service-authentication attempt."""

    ok: bool
    code: str
    status_code: int
    principal: ServicePrincipal
    correlation: RequestCorrelationContext
    error: str = ""
    retryable: bool = False
    details: Mapping[str, Any] = field(default_factory=dict)
    elapsed_ms: float = 0.0
    schema_version: str = SERVICE_AUTH_RESULT_SCHEMA_VERSION

    def to_dict(self, *, include_private: bool = False) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "schema_version": self.schema_version,
            "schemaVersion": self.schema_version,
            "ok": self.ok,
            "code": self.code,
            "status_code": self.status_code,
            "statusCode": self.status_code,
            "retryable": self.retryable,
            "principal": self.principal.to_dict(include_private=include_private),
            "request": self.correlation.to_dict(include_idempotency_key=False),
            "elapsed_ms": round(max(0.0, float(self.elapsed_ms)), 3),
            "elapsedMs": round(max(0.0, float(self.elapsed_ms)), 3),
        }
        if self.error:
            payload["error"] = self.error
        if self.details:
            payload["details"] = sanitize_diagnostic_mapping(self.details)
        return payload

    def raise_for_error(self) -> "ServiceAuthResult":
        if self.ok:
            return self
        raise ServiceAuthenticationError(
            self.error or "Service authentication failed.",
            code=self.code,
            status_code=self.status_code,
            retryable=self.retryable,
            details=self.details,
            request_id=self.correlation.request_id,
            correlation_id=self.correlation.correlation_id,
        )


@dataclass(frozen=True, slots=True)
class _CredentialCandidate:
    header_name: str
    scheme: str
    token: str = field(repr=False)

    @property
    def fingerprint(self) -> str:
        return _secret_fingerprint(self.token)


@dataclass(frozen=True, slots=True)
class _ServiceAuthSettings:
    required: bool
    configured_secret: str = field(repr=False)
    allowed_service_ids: tuple[str, ...]
    service_id_headers: tuple[str, ...]
    credential_headers: tuple[str, ...]
    exempt_paths: tuple[str, ...]
    allow_bearer: bool
    max_token_length: int
    request_id_header: str
    correlation_id_header: str
    idempotency_key_header: str
    max_request_id_length: int
    max_idempotency_key_length: int

    def to_redacted_dict(self) -> dict[str, Any]:
        return {
            "required": self.required,
            "configured": bool(self.configured_secret),
            "credential_fingerprint": _secret_fingerprint(self.configured_secret),
            "allowed_service_ids": list(self.allowed_service_ids),
            "service_id_headers": list(self.service_id_headers),
            "credential_headers": list(self.credential_headers),
            "exempt_paths": list(self.exempt_paths),
            "allow_bearer": self.allow_bearer,
            "max_token_length": self.max_token_length,
            "request_id_header": self.request_id_header,
            "correlation_id_header": self.correlation_id_header,
            "idempotency_key_header": self.idempotency_key_header,
            "max_request_id_length": self.max_request_id_length,
            "max_idempotency_key_length": self.max_idempotency_key_length,
        }


def _normalize_text(value: Any, default: str = "", maximum: int = 8192) -> str:
    try:
        if value is None:
            return default
        text = str(value).strip()
        if not text:
            return default
        if maximum > 0:
            text = text[:maximum]
        return text
    except Exception:
        return default


def _bounded_int(value: Any, default: int, minimum: int, maximum: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError, OverflowError):
        number = int(default)
    return max(minimum, min(maximum, number))


def _safe_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return bool(value)
    text = _normalize_text(value, "", 32).lower()
    if text in {"1", "true", "yes", "y", "on", "enabled", "enable", "active"}:
        return True
    if text in {"0", "false", "no", "n", "off", "disabled", "disable", "inactive"}:
        return False
    return bool(default)


def _safe_code(value: Any, default: str) -> str:
    text = _normalize_text(value, default, 120).lower().replace("-", "_").replace(" ", "_")
    cleaned = re.sub(r"[^a-z0-9_]+", "_", text).strip("_")
    return cleaned or default


def _normalize_header_name(value: Any, default: str = "") -> str:
    text = _normalize_text(value, default, 128)
    if not text or not _HEADER_NAME_RE.fullmatch(text):
        return default
    return "-".join(part[:1].upper() + part[1:].lower() for part in text.split("-") if part)


def _normalize_header_names(values: Any, defaults: Sequence[str]) -> tuple[str, ...]:
    items: list[Any]
    if values is None:
        items = list(defaults)
    elif isinstance(values, str):
        items = [item.strip() for item in values.split(",")]
    else:
        try:
            items = list(values)
        except Exception:
            items = list(defaults)

    result: list[str] = []
    seen: set[str] = set()
    for item in items:
        normalized = _normalize_header_name(item, "")
        lowered = normalized.lower()
        if normalized and lowered not in seen:
            seen.add(lowered)
            result.append(normalized)
    if not result:
        return tuple(defaults)
    return tuple(result[:16])


def normalize_service_id(value: Any, default: str = "") -> str:
    """Normalize a calling-service identifier without accepting paths or URLs."""
    text = _normalize_text(value, default, DEFAULT_MAX_SERVICE_ID_LENGTH).lower()
    if not text:
        return default
    text = _WHITESPACE_RE.sub("-", text)
    text = _SAFE_IDENTIFIER_RE.sub("-", text)
    text = _MULTI_SEPARATOR_RE.sub("-", text).strip("-_.:")
    if not text or not _SERVICE_ID_RE.fullmatch(text):
        return default
    return text


def _normalize_service_ids(values: Any, defaults: Sequence[str]) -> tuple[str, ...]:
    if values is None:
        raw_items: list[Any] = list(defaults)
    elif isinstance(values, str):
        raw_items = [item.strip() for item in values.split(",")]
    else:
        try:
            raw_items = list(values)
        except Exception:
            raw_items = list(defaults)

    result: list[str] = []
    seen: set[str] = set()
    for raw in raw_items:
        item = normalize_service_id(raw, "")
        if item and item not in seen:
            seen.add(item)
            result.append(item)
    return tuple(result or tuple(defaults))


def _normalize_path(value: Any, default: str = "/") -> str:
    text = _normalize_text(value, default, 2048)
    if not text:
        return default
    try:
        split = urlsplit(text)
        path = split.path or "/"
    except Exception:
        path = text.split("?", 1)[0].split("#", 1)[0] or "/"
    if not path.startswith("/"):
        path = "/" + path
    path = re.sub(r"/{2,}", "/", path)
    if len(path) > 1 and path.endswith("/"):
        path = path[:-1]
    return path or "/"


def _normalize_exempt_paths(values: Any) -> tuple[str, ...]:
    if values is None:
        raw_items: list[Any] = list(DEFAULT_EXEMPT_PATHS)
    elif isinstance(values, str):
        raw_items = [item.strip() for item in values.split(",")]
    else:
        try:
            raw_items = list(values)
        except Exception:
            raw_items = list(DEFAULT_EXEMPT_PATHS)

    result: list[str] = []
    seen: set[str] = set()
    for item in raw_items:
        text = _normalize_text(item, "", 256)
        if not text:
            continue
        wildcard = text.endswith("/*")
        normalized = _normalize_path(text[:-2] if wildcard else text, "/")
        final = normalized + "/*" if wildcard and normalized != "/" else normalized
        if final not in seen:
            seen.add(final)
            result.append(final)
    return tuple(result or DEFAULT_EXEMPT_PATHS)


def is_service_auth_exempt_path(path: Any, exempt_paths: Optional[Iterable[str]] = None) -> bool:
    """Return whether an exact or explicitly wildcarded path is exempt."""
    normalized_path = _normalize_path(path, "/")
    patterns = _normalize_exempt_paths(exempt_paths)
    for pattern in patterns:
        try:
            if pattern.endswith("/*"):
                prefix = pattern[:-2]
                if normalized_path == prefix or normalized_path.startswith(prefix + "/"):
                    return True
            elif normalized_path == pattern:
                return True
        except Exception:
            continue
    return False


def _secret_fingerprint(value: Any) -> str:
    text = _normalize_text(value, "", 16384)
    if not text:
        return ""
    try:
        return "sha256:" + hashlib.sha256(text.encode("utf-8", "surrogatepass")).hexdigest()[:20]
    except Exception:
        return "sha256:unavailable"


def _hash_idempotency_key(value: Any) -> str:
    text = _normalize_text(value, "", 8192)
    if not text:
        return ""
    try:
        return hashlib.sha256(text.encode("utf-8", "surrogatepass")).hexdigest()
    except Exception:
        return ""


def _constant_time_equal(left: Any, right: Any) -> bool:
    left_text = _normalize_text(left, "", 16384)
    right_text = _normalize_text(right, "", 16384)
    try:
        return hmac.compare_digest(
            left_text.encode("utf-8", "surrogatepass"),
            right_text.encode("utf-8", "surrogatepass"),
        )
    except Exception:
        return False


def _is_sensitive_key(key: Any) -> bool:
    lowered = _normalize_text(key, "", 256).lower().replace("-", "_")
    return any(fragment in lowered for fragment in _SECRET_KEY_FRAGMENTS)


def _is_identity_key(key: Any) -> bool:
    lowered = _normalize_text(key, "", 256).lower().replace("-", "_")
    return any(fragment in lowered for fragment in _IDENTITY_KEY_FRAGMENTS)


def redact_diagnostic_text(value: Any, maximum: int = 2000) -> str:
    """Redact obvious credentials and control characters from diagnostic text."""
    text = _normalize_text(value, "", maximum * 2 if maximum > 0 else 4096)
    if not text:
        return ""
    text = _CONTROL_CHAR_RE.sub(" ", text)
    text = re.sub(
        r"(?i)(authorization|x-api-key|x-vectoplan-internal-token|token|password|secret)\s*[:=]\s*[^\s,;]+",
        r"\1=[REDACTED]",
        text,
    )
    text = re.sub(r"(?i)bearer\s+[A-Za-z0-9._~+/=-]+", "Bearer [REDACTED]", text)
    return text[:maximum] if maximum > 0 else text


def sanitize_diagnostic_value(value: Any, *, depth: int = 0) -> Any:
    if depth > 5:
        return "[TRUNCATED]"
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return redact_diagnostic_text(value, 2000)
    if isinstance(value, Mapping):
        return sanitize_diagnostic_mapping(value, depth=depth + 1)
    if isinstance(value, (list, tuple, set, frozenset)):
        result: list[Any] = []
        for item in list(value)[:50]:
            result.append(sanitize_diagnostic_value(item, depth=depth + 1))
        return result
    try:
        return redact_diagnostic_text(str(value), 1000)
    except Exception:
        return "[UNSERIALIZABLE]"


def sanitize_diagnostic_mapping(
    value: Optional[Mapping[str, Any]],
    *,
    depth: int = 0,
) -> dict[str, Any]:
    result: dict[str, Any] = {}
    if not isinstance(value, Mapping):
        return result
    for index, (raw_key, raw_value) in enumerate(value.items()):
        if index >= 100:
            result["_truncated"] = True
            break
        key = _normalize_text(raw_key, "", 160)
        if not key:
            continue
        if _is_sensitive_key(key):
            result[key] = "[REDACTED]"
        elif _is_identity_key(key):
            result[key] = "[REDACTED_IDENTITY]"
        else:
            result[key] = sanitize_diagnostic_value(raw_value, depth=depth + 1)
    return result


def _mapping_get_case_insensitive(headers: Any, name: str) -> Any:
    if headers is None:
        return None
    try:
        getter = getattr(headers, "get", None)
        if callable(getter):
            direct = getter(name)
            if direct is not None:
                return direct
    except Exception:
        pass
    target = name.lower()
    try:
        items = headers.items() if hasattr(headers, "items") else headers
        for raw_key, raw_value in items:
            if _normalize_text(raw_key, "", 256).lower() == target:
                return raw_value
    except Exception:
        return None
    return None


def _config_value(config: Any, name: str, default: Any = None) -> Any:
    if config is None:
        return default
    try:
        if isinstance(config, Mapping):
            return config.get(name, default)
    except Exception:
        pass
    try:
        return getattr(config, name, default)
    except Exception:
        return default


def _resolve_default_config() -> Any:
    """Resolve Flask app config or the configured Chunk config class lazily."""
    try:
        from flask import current_app

        try:
            if current_app:
                return current_app.config
        except Exception:
            pass
    except Exception:
        pass

    try:
        from config import get_config_class

        return get_config_class()
    except Exception:
        return None


def _build_settings(config: Any = None) -> _ServiceAuthSettings:
    resolved = config if config is not None else _resolve_default_config()
    required = _safe_bool(
        _config_value(resolved, "VECTOPLAN_CHUNK_SERVICE_AUTH_REQUIRED", True),
        True,
    )
    secret = _normalize_text(
        _config_value(resolved, "VECTOPLAN_CHUNK_SERVICE_API_KEY", ""),
        "",
        16384,
    )
    allowed = _normalize_service_ids(
        _config_value(
            resolved,
            "VECTOPLAN_CHUNK_ALLOWED_SERVICE_IDS",
            DEFAULT_ALLOWED_SERVICE_IDS,
        ),
        DEFAULT_ALLOWED_SERVICE_IDS,
    )

    primary_service_header = _normalize_header_name(
        _config_value(
            resolved,
            "VECTOPLAN_CHUNK_SERVICE_ID_HEADER",
            DEFAULT_SERVICE_ID_HEADER,
        ),
        DEFAULT_SERVICE_ID_HEADER,
    )
    service_headers = list(
        _normalize_header_names(
            _config_value(
                resolved,
                "VECTOPLAN_CHUNK_SERVICE_ID_HEADERS",
                DEFAULT_SERVICE_ID_HEADERS,
            ),
            DEFAULT_SERVICE_ID_HEADERS,
        )
    )
    if primary_service_header.lower() not in {item.lower() for item in service_headers}:
        service_headers.insert(0, primary_service_header)

    primary_credential_header = _normalize_header_name(
        _config_value(
            resolved,
            "VECTOPLAN_CHUNK_SERVICE_API_KEY_HEADER",
            DEFAULT_CREDENTIAL_HEADER,
        ),
        DEFAULT_CREDENTIAL_HEADER,
    )
    credential_headers = list(
        _normalize_header_names(
            _config_value(
                resolved,
                "VECTOPLAN_CHUNK_SERVICE_API_KEY_HEADERS",
                DEFAULT_CREDENTIAL_HEADERS,
            ),
            DEFAULT_CREDENTIAL_HEADERS,
        )
    )
    if primary_credential_header.lower() not in {item.lower() for item in credential_headers}:
        credential_headers.insert(0, primary_credential_header)

    return _ServiceAuthSettings(
        required=required,
        configured_secret=secret,
        allowed_service_ids=allowed,
        service_id_headers=tuple(service_headers[:16]),
        credential_headers=tuple(credential_headers[:16]),
        exempt_paths=_normalize_exempt_paths(
            _config_value(
                resolved,
                "VECTOPLAN_CHUNK_SERVICE_AUTH_EXEMPT_PATHS",
                DEFAULT_EXEMPT_PATHS,
            )
        ),
        allow_bearer=_safe_bool(
            _config_value(
                resolved,
                "VECTOPLAN_CHUNK_SERVICE_AUTH_ALLOW_BEARER",
                True,
            ),
            True,
        ),
        max_token_length=_bounded_int(
            _config_value(
                resolved,
                "VECTOPLAN_CHUNK_SERVICE_AUTH_MAX_TOKEN_LENGTH",
                DEFAULT_MAX_TOKEN_LENGTH,
            ),
            DEFAULT_MAX_TOKEN_LENGTH,
            32,
            16384,
        ),
        request_id_header=_normalize_header_name(
            _config_value(
                resolved,
                "VECTOPLAN_CHUNK_REQUEST_ID_HEADER",
                DEFAULT_REQUEST_ID_HEADER,
            ),
            DEFAULT_REQUEST_ID_HEADER,
        ),
        correlation_id_header=_normalize_header_name(
            _config_value(
                resolved,
                "VECTOPLAN_CHUNK_CORRELATION_ID_HEADER",
                DEFAULT_CORRELATION_ID_HEADER,
            ),
            DEFAULT_CORRELATION_ID_HEADER,
        ),
        idempotency_key_header=_normalize_header_name(
            _config_value(
                resolved,
                "VECTOPLAN_CHUNK_IDEMPOTENCY_KEY_HEADER",
                DEFAULT_IDEMPOTENCY_KEY_HEADER,
            ),
            DEFAULT_IDEMPOTENCY_KEY_HEADER,
        ),
        max_request_id_length=_bounded_int(
            _config_value(
                resolved,
                "VECTOPLAN_CHUNK_MAX_REQUEST_ID_LENGTH",
                DEFAULT_MAX_REQUEST_ID_LENGTH,
            ),
            DEFAULT_MAX_REQUEST_ID_LENGTH,
            32,
            1024,
        ),
        max_idempotency_key_length=_bounded_int(
            _config_value(
                resolved,
                "VECTOPLAN_CHUNK_MAX_IDEMPOTENCY_KEY_LENGTH",
                DEFAULT_MAX_IDEMPOTENCY_KEY_LENGTH,
            ),
            DEFAULT_MAX_IDEMPOTENCY_KEY_LENGTH,
            32,
            2048,
        ),
    )


def _normalize_request_identifier(
    value: Any,
    *,
    prefix: str,
    maximum: int,
    generate: bool,
) -> str:
    text = _normalize_text(value, "", maximum * 2)
    if text and not _CONTROL_CHAR_RE.search(text):
        text = _WHITESPACE_RE.sub("-", text)
        text = _SAFE_IDENTIFIER_RE.sub("-", text)
        text = _MULTI_SEPARATOR_RE.sub("-", text).strip("-_.:")
        if text:
            return text[:maximum]
    if not generate:
        return ""
    safe_prefix = normalize_service_id(prefix, "req") or "req"
    return f"{safe_prefix}_{uuid.uuid4().hex}"[:maximum]


def _normalize_idempotency_key(value: Any, maximum: int) -> str:
    text = _normalize_text(value, "", maximum * 2)
    if not text:
        return ""
    if _CONTROL_CHAR_RE.search(text):
        return ""
    text = _WHITESPACE_RE.sub("-", text)
    text = _SAFE_IDENTIFIER_RE.sub("-", text)
    text = _MULTI_SEPARATOR_RE.sub("-", text).strip("-_.:")
    return text[:maximum]


def build_request_correlation_context(
    headers: Any = None,
    *,
    config: Any = None,
    request_id: Any = None,
    correlation_id: Any = None,
    idempotency_key: Any = None,
) -> RequestCorrelationContext:
    """Build normalized request/correlation/idempotency identifiers."""
    settings = _build_settings(config)

    request_header_value = request_id
    request_source = "argument"
    if request_header_value is None:
        request_header_value = _mapping_get_case_insensitive(headers, settings.request_id_header)
        request_source = "header" if request_header_value else "generated"

    resolved_request_id = _normalize_request_identifier(
        request_header_value,
        prefix="req",
        maximum=settings.max_request_id_length,
        generate=True,
    )

    correlation_header_value = correlation_id
    correlation_source = "argument"
    if correlation_header_value is None:
        correlation_header_value = _mapping_get_case_insensitive(
            headers,
            settings.correlation_id_header,
        )
        correlation_source = "header" if correlation_header_value else "request_id"

    resolved_correlation_id = _normalize_request_identifier(
        correlation_header_value,
        prefix="corr",
        maximum=settings.max_request_id_length,
        generate=False,
    ) or resolved_request_id

    idempotency_header_value = idempotency_key
    if idempotency_header_value is None:
        idempotency_header_value = _mapping_get_case_insensitive(
            headers,
            settings.idempotency_key_header,
        )
    resolved_idempotency_key = _normalize_idempotency_key(
        idempotency_header_value,
        settings.max_idempotency_key_length,
    )

    return RequestCorrelationContext(
        request_id=resolved_request_id,
        correlation_id=resolved_correlation_id,
        idempotency_key=resolved_idempotency_key,
        idempotency_key_hash=_hash_idempotency_key(resolved_idempotency_key),
        request_id_source=request_source,
        correlation_id_source=correlation_source,
        idempotency_key_present=bool(resolved_idempotency_key),
    )


def _resolve_request_parts(request_obj: Any = None) -> tuple[Any, str, str]:
    request_value = request_obj
    if request_value is None:
        try:
            from flask import request as flask_request

            request_value = flask_request
        except Exception:
            request_value = None

    headers = None
    path = "/"
    method = "GET"
    if request_value is not None:
        try:
            headers = getattr(request_value, "headers", None)
        except Exception:
            headers = None
        try:
            path = getattr(request_value, "path", None) or getattr(
                request_value,
                "full_path",
                "/",
            )
        except Exception:
            path = "/"
        try:
            method = _normalize_text(getattr(request_value, "method", "GET"), "GET", 32).upper()
        except Exception:
            method = "GET"
    return headers, _normalize_path(path, "/"), method


def _resolve_service_id(headers: Any, settings: _ServiceAuthSettings) -> tuple[str, str]:
    candidates: list[tuple[str, str]] = []
    for header_name in settings.service_id_headers:
        raw_value = _mapping_get_case_insensitive(headers, header_name)
        if raw_value is None:
            continue
        raw_text = _normalize_text(raw_value, "", DEFAULT_MAX_SERVICE_ID_LENGTH * 2)
        if raw_text:
            candidates.append((header_name, raw_text))

    if not candidates:
        return "", ""

    normalized_pairs: list[tuple[str, str]] = []
    for header_name, raw_text in candidates:
        normalized = normalize_service_id(raw_text, "")
        if not normalized:
            raise ServiceAuthenticationError(
                "The calling service identifier is invalid.",
                code=AUTH_CODE_INVALID_SERVICE_ID,
                status_code=400,
                details={"header": header_name},
            )
        normalized_pairs.append((header_name, normalized))

    unique_values = {item[1] for item in normalized_pairs}
    if len(unique_values) > 1:
        raise ServiceAuthenticationError(
            "Conflicting calling service identifiers were supplied.",
            code=AUTH_CODE_CONFLICTING_SERVICE_ID,
            status_code=400,
            details={"headers": [item[0] for item in normalized_pairs]},
        )

    return normalized_pairs[0][1], normalized_pairs[0][0]


def _parse_authorization_value(
    raw_value: Any,
    *,
    allow_bearer: bool,
) -> tuple[str, str]:
    text = _normalize_text(raw_value, "", 16384)
    if not text:
        return "", ""
    parts = text.split(None, 1)
    if len(parts) != 2:
        raise ServiceAuthenticationError(
            "Authorization must use an explicit credential scheme.",
            code=AUTH_CODE_UNSUPPORTED_SCHEME,
            status_code=401,
        )
    scheme = parts[0].lower()
    token = parts[1].strip()
    if scheme == "bearer":
        if not allow_bearer:
            raise ServiceAuthenticationError(
                "Bearer service credentials are disabled.",
                code=AUTH_CODE_UNSUPPORTED_SCHEME,
                status_code=401,
            )
        return "bearer", token
    if scheme in {"apikey", "api-key", "internal"}:
        return "api_key", token
    raise ServiceAuthenticationError(
        "The Authorization credential scheme is not supported.",
        code=AUTH_CODE_UNSUPPORTED_SCHEME,
        status_code=401,
        details={"scheme": scheme[:32]},
    )


def _resolve_credential(headers: Any, settings: _ServiceAuthSettings) -> _CredentialCandidate | None:
    candidates: list[_CredentialCandidate] = []
    for header_name in settings.credential_headers:
        raw_value = _mapping_get_case_insensitive(headers, header_name)
        if raw_value is None:
            continue
        raw_text = _normalize_text(raw_value, "", settings.max_token_length * 2)
        if not raw_text:
            continue

        if header_name.lower() == "authorization":
            scheme, token = _parse_authorization_value(
                raw_text,
                allow_bearer=settings.allow_bearer,
            )
        else:
            scheme, token = "api_key", raw_text

        if not token:
            continue
        if len(token) > settings.max_token_length:
            raise ServiceAuthenticationError(
                "The service credential exceeds the configured maximum length.",
                code=AUTH_CODE_CREDENTIAL_TOO_LONG,
                status_code=400,
                details={"header": header_name},
            )
        if _CONTROL_CHAR_RE.search(token):
            raise ServiceAuthenticationError(
                "The service credential contains invalid control characters.",
                code=AUTH_CODE_INVALID_CREDENTIAL,
                status_code=400,
                details={"header": header_name},
            )
        candidates.append(
            _CredentialCandidate(
                header_name=header_name,
                scheme=scheme,
                token=token,
            )
        )

    if not candidates:
        return None

    fingerprints = {candidate.fingerprint for candidate in candidates}
    if len(fingerprints) > 1:
        raise ServiceAuthenticationError(
            "Conflicting service credentials were supplied.",
            code=AUTH_CODE_CONFLICTING_CREDENTIAL,
            status_code=400,
            details={"headers": [candidate.header_name for candidate in candidates]},
        )
    return candidates[0]


def _build_principal(
    *,
    service_id: str,
    authenticated: bool,
    auth_required: bool,
    exempt: bool,
    credential: _CredentialCandidate | None,
    correlation: RequestCorrelationContext,
    auth_source: str,
) -> ServicePrincipal:
    return ServicePrincipal(
        service_id=service_id,
        authenticated=authenticated,
        auth_required=auth_required,
        exempt=exempt,
        credential_scheme=credential.scheme if credential else "",
        credential_header=credential.header_name if credential else "",
        credential_fingerprint=credential.fingerprint if credential else "",
        request_id=correlation.request_id,
        correlation_id=correlation.correlation_id,
        idempotency_key_hash=correlation.idempotency_key_hash,
        auth_source=auth_source,
    )


def _result_from_error(
    error: ServiceAuthenticationError,
    *,
    correlation: RequestCorrelationContext,
    started_at: float,
    required: bool,
) -> ServiceAuthResult:
    principal = _build_principal(
        service_id="",
        authenticated=False,
        auth_required=required,
        exempt=False,
        credential=None,
        correlation=correlation,
        auth_source="authentication_failed",
    )
    return ServiceAuthResult(
        ok=False,
        code=error.code,
        status_code=error.status_code,
        principal=principal,
        correlation=correlation,
        error=redact_diagnostic_text(str(error), 1000),
        retryable=error.retryable,
        details=error.details,
        elapsed_ms=(time.perf_counter() - started_at) * 1000.0,
    )


def authenticate_service_headers(
    headers: Any,
    *,
    path: Any = "/",
    method: Any = "GET",
    config: Any = None,
    required: Optional[bool] = None,
    allow_exempt: bool = True,
    request_id: Any = None,
    correlation_id: Any = None,
    idempotency_key: Any = None,
    raise_on_error: bool = False,
) -> ServiceAuthResult:
    """Authenticate an explicit header mapping without requiring Flask."""
    started_at = time.perf_counter()
    settings = _build_settings(config)
    auth_required = settings.required if required is None else bool(required)
    correlation = build_request_correlation_context(
        headers,
        config=config,
        request_id=request_id,
        correlation_id=correlation_id,
        idempotency_key=idempotency_key,
    )
    normalized_path = _normalize_path(path, "/")
    normalized_method = _normalize_text(method, "GET", 32).upper()

    try:
        if allow_exempt and is_service_auth_exempt_path(normalized_path, settings.exempt_paths):
            principal = _build_principal(
                service_id="",
                authenticated=False,
                auth_required=auth_required,
                exempt=True,
                credential=None,
                correlation=correlation,
                auth_source="exempt_path",
            )
            result = ServiceAuthResult(
                ok=True,
                code=AUTH_CODE_EXEMPT,
                status_code=200,
                principal=principal,
                correlation=correlation,
                details={"path": normalized_path, "method": normalized_method},
                elapsed_ms=(time.perf_counter() - started_at) * 1000.0,
            )
            return result

        service_id, service_id_header = _resolve_service_id(headers, settings)
        credential = _resolve_credential(headers, settings)

        if not auth_required:
            if not service_id and credential is None:
                principal = _build_principal(
                    service_id="",
                    authenticated=False,
                    auth_required=False,
                    exempt=False,
                    credential=None,
                    correlation=correlation,
                    auth_source="auth_disabled",
                )
                return ServiceAuthResult(
                    ok=True,
                    code=AUTH_CODE_DISABLED,
                    status_code=200,
                    principal=principal,
                    correlation=correlation,
                    details={"path": normalized_path, "method": normalized_method},
                    elapsed_ms=(time.perf_counter() - started_at) * 1000.0,
                )
            # Optional mode still validates supplied credentials rather than ignoring them.

        if not service_id:
            raise ServiceAuthenticationError(
                "The calling service identifier is required.",
                code=AUTH_CODE_MISSING_SERVICE_ID,
                status_code=401,
                details={"accepted_headers": list(settings.service_id_headers)},
            )

        if service_id not in settings.allowed_service_ids:
            raise ServiceAuthenticationError(
                "The calling service is not allowed.",
                code=AUTH_CODE_SERVICE_NOT_ALLOWED,
                status_code=403,
                details={"service_id": service_id},
            )

        if credential is None:
            raise ServiceAuthenticationError(
                "A service credential is required.",
                code=AUTH_CODE_MISSING_CREDENTIAL,
                status_code=401,
                details={"accepted_headers": list(settings.credential_headers)},
            )

        if not settings.configured_secret:
            raise ServiceAuthenticationError(
                "Service authentication is required but no server credential is configured.",
                code=AUTH_CODE_AUTH_NOT_CONFIGURED,
                status_code=503,
                retryable=False,
            )

        if not _constant_time_equal(credential.token, settings.configured_secret):
            raise ServiceAuthenticationError(
                "The service credential is invalid.",
                code=AUTH_CODE_INVALID_CREDENTIAL,
                status_code=401,
                details={"service_id": service_id, "credential_header": credential.header_name},
            )

        principal = _build_principal(
            service_id=service_id,
            authenticated=True,
            auth_required=auth_required,
            exempt=False,
            credential=credential,
            correlation=correlation,
            auth_source="shared_service_key",
        )
        return ServiceAuthResult(
            ok=True,
            code=AUTH_CODE_OK,
            status_code=200,
            principal=principal,
            correlation=correlation,
            details={
                "service_id_header": service_id_header,
                "path": normalized_path,
                "method": normalized_method,
            },
            elapsed_ms=(time.perf_counter() - started_at) * 1000.0,
        )
    except ServiceAuthenticationError as exc:
        result = _result_from_error(
            exc,
            correlation=correlation,
            started_at=started_at,
            required=auth_required,
        )
        if raise_on_error:
            result.raise_for_error()
        return result
    except Exception as exc:
        LOGGER.exception("Unexpected service-authentication failure")
        error = ServiceAuthenticationError(
            "Service authentication failed because of an internal error.",
            code=AUTH_CODE_INTERNAL_ERROR,
            status_code=500,
            retryable=False,
            details={"exception_type": type(exc).__name__},
        )
        result = _result_from_error(
            error,
            correlation=correlation,
            started_at=started_at,
            required=auth_required,
        )
        if raise_on_error:
            result.raise_for_error()
        return result


def authenticate_service_request(
    request_obj: Any = None,
    *,
    config: Any = None,
    required: Optional[bool] = None,
    allow_exempt: bool = True,
    raise_on_error: bool = False,
    store_in_context: bool = True,
) -> ServiceAuthResult:
    """Authenticate a Flask-like request object and optionally store its principal."""
    headers, path, method = _resolve_request_parts(request_obj)
    result = authenticate_service_headers(
        headers,
        path=path,
        method=method,
        config=config,
        required=required,
        allow_exempt=allow_exempt,
        raise_on_error=raise_on_error,
    )
    if store_in_context:
        store_service_auth_result(result)
    return result


def store_service_auth_result(result: ServiceAuthResult) -> bool:
    """Store the result in Flask ``g`` when a request context exists."""
    try:
        from flask import g

        g.vectoplan_service_auth_result = result
        g.vectoplan_service_principal = result.principal
        g.vectoplan_request_correlation = result.correlation
        return True
    except Exception:
        return False


def get_current_service_auth_result(default: Any = None) -> ServiceAuthResult | Any:
    try:
        from flask import g

        return getattr(g, "vectoplan_service_auth_result", default)
    except Exception:
        return default


def get_current_service_principal(default: Any = None) -> ServicePrincipal | Any:
    try:
        from flask import g

        return getattr(g, "vectoplan_service_principal", default)
    except Exception:
        return default


def get_current_request_correlation(default: Any = None) -> RequestCorrelationContext | Any:
    try:
        from flask import g

        return getattr(g, "vectoplan_request_correlation", default)
    except Exception:
        return default


def require_authenticated_service(
    *,
    allowed_service_ids: Optional[Iterable[str]] = None,
    principal: Optional[ServicePrincipal] = None,
) -> ServicePrincipal:
    """Require an authenticated non-exempt service principal."""
    resolved = principal or get_current_service_principal(None)
    if not isinstance(resolved, ServicePrincipal) or not resolved.is_trusted_service:
        correlation = get_current_request_correlation(None)
        raise ServiceAuthenticationError(
            "An authenticated internal service is required.",
            code=AUTH_CODE_MISSING_CREDENTIAL,
            status_code=401,
            request_id=getattr(correlation, "request_id", ""),
            correlation_id=getattr(correlation, "correlation_id", ""),
        )
    if allowed_service_ids is not None:
        allowed = set(_normalize_service_ids(allowed_service_ids, ()))
        if not allowed or resolved.service_id not in allowed:
            raise ServiceAuthenticationError(
                "The authenticated service is not allowed for this operation.",
                code=AUTH_CODE_SERVICE_NOT_ALLOWED,
                status_code=403,
                details={"service_id": resolved.service_id},
                request_id=resolved.request_id,
                correlation_id=resolved.correlation_id,
            )
    return resolved


def build_service_auth_error_response(
    error_or_result: ServiceAuthenticationError | ServiceAuthResult,
) -> Any:
    """Build a Flask response when available, otherwise ``(dict, status)``."""
    if isinstance(error_or_result, ServiceAuthResult):
        result = error_or_result
        payload = result.to_dict(include_private=False)
        status_code = result.status_code
        correlation = result.correlation
    else:
        payload = error_or_result.to_dict()
        status_code = error_or_result.status_code
        correlation = RequestCorrelationContext(
            request_id=error_or_result.request_id
            or _normalize_request_identifier(
                None,
                prefix="req",
                maximum=DEFAULT_MAX_REQUEST_ID_LENGTH,
                generate=True,
            ),
            correlation_id=error_or_result.correlation_id
            or error_or_result.request_id
            or _normalize_request_identifier(
                None,
                prefix="corr",
                maximum=DEFAULT_MAX_REQUEST_ID_LENGTH,
                generate=True,
            ),
        )
    try:
        from flask import jsonify, make_response

        response = make_response(jsonify(payload), status_code)
        apply_correlation_response_headers(response, correlation=correlation)
        response.headers.setdefault("Cache-Control", "no-store")
        return response
    except Exception:
        return payload, status_code


def apply_correlation_response_headers(
    response: Any,
    *,
    correlation: Optional[RequestCorrelationContext] = None,
    config: Any = None,
) -> Any:
    """Add safe correlation headers to a Flask-like response object."""
    resolved = correlation or get_current_request_correlation(None)
    if not isinstance(resolved, RequestCorrelationContext):
        resolved = build_request_correlation_context(config=config)
    settings = _build_settings(config)
    try:
        headers = getattr(response, "headers", None)
        if headers is None and isinstance(response, MutableMapping):
            headers = response
        if headers is not None:
            headers[settings.request_id_header] = resolved.request_id
            headers[settings.correlation_id_header] = resolved.correlation_id
    except Exception:
        pass
    return response


def service_auth_required(
    view_func: Optional[Callable[..., Any]] = None,
    *,
    allowed_service_ids: Optional[Iterable[str]] = None,
    allow_exempt: bool = False,
    config: Any = None,
) -> Callable[..., Any]:
    """Flask route decorator enforcing inbound service authentication."""

    def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
        @functools.wraps(func)
        def wrapped(*args: Any, **kwargs: Any) -> Any:
            result = authenticate_service_request(
                config=config,
                required=True,
                allow_exempt=allow_exempt,
                raise_on_error=False,
                store_in_context=True,
            )
            if not result.ok:
                return build_service_auth_error_response(result)
            try:
                require_authenticated_service(
                    allowed_service_ids=allowed_service_ids,
                    principal=result.principal,
                )
            except ServiceAuthenticationError as exc:
                return build_service_auth_error_response(exc)
            response = func(*args, **kwargs)
            try:
                try:
                    from flask import make_response

                    response = make_response(response)
                except Exception:
                    pass
                return apply_correlation_response_headers(
                    response,
                    correlation=result.correlation,
                    config=config,
                )
            except Exception:
                return response

        return wrapped

    if view_func is not None:
        return decorator(view_func)
    return decorator


def install_service_auth(
    app: Any,
    *,
    guard_all_requests: bool = False,
    allow_exempt: bool = True,
) -> bool:
    """
    Install request correlation and optional global service-auth enforcement.

    ``guard_all_requests`` defaults to ``False`` so route modules can adopt the
    decorator incrementally.  When enabled, every non-exempt request is guarded.
    """
    if app is None:
        return False
    try:
        marker = "_vectoplan_service_auth_installed"
        if getattr(app, marker, False):
            return True

        @app.before_request
        def _vectoplan_service_auth_before_request() -> Any:
            try:
                result = authenticate_service_request(
                    config=getattr(app, "config", None),
                    required=None if guard_all_requests else False,
                    allow_exempt=allow_exempt,
                    raise_on_error=False,
                    store_in_context=True,
                )
                if guard_all_requests and not result.ok:
                    return build_service_auth_error_response(result)
                return None
            except Exception as exc:
                LOGGER.exception("Global service-auth before-request hook failed")
                if guard_all_requests:
                    error = ServiceAuthenticationError(
                        "Service authentication failed because of an internal error.",
                        code=AUTH_CODE_INTERNAL_ERROR,
                        status_code=500,
                        details={"exception_type": type(exc).__name__},
                    )
                    return build_service_auth_error_response(error)
                return None

        @app.after_request
        def _vectoplan_service_auth_after_request(response: Any) -> Any:
            try:
                return apply_correlation_response_headers(
                    response,
                    config=getattr(app, "config", None),
                )
            except Exception:
                return response

        setattr(app, marker, True)
        return True
    except Exception:
        LOGGER.exception("Could not install service authentication")
        return False


def get_service_auth_status(config: Any = None) -> dict[str, Any]:
    """Return redacted configuration and runtime capability diagnostics."""
    try:
        settings = _build_settings(config)
        errors: list[str] = []
        warnings: list[str] = []
        if settings.required and not settings.configured_secret:
            errors.append("service credential is required but not configured")
        if settings.required and not settings.allowed_service_ids:
            errors.append("service allow-list is empty")
        if not settings.required:
            warnings.append("service authentication is disabled")
        if settings.configured_secret and (
            settings.configured_secret.startswith("dev-")
            or "change-me" in settings.configured_secret.lower()
        ):
            warnings.append("development service credential is configured")

        return {
            "ok": not errors,
            "service": "vectoplan-chunk",
            "component": "service_auth_service",
            "version": SERVICE_AUTH_VERSION,
            "errors": errors,
            "warnings": warnings,
            "config": settings.to_redacted_dict(),
            "capabilities": {
                "constant_time_secret_compare": True,
                "flask_optional": True,
                "global_guard_supported": True,
                "route_decorator_supported": True,
                "request_correlation": True,
                "idempotency_key_hashing": True,
                "user_authentication": False,
                "project_authorization": False,
                "outbound_calls": False,
                "database_access": False,
            },
        }
    except Exception as exc:
        return {
            "ok": False,
            "service": "vectoplan-chunk",
            "component": "service_auth_service",
            "version": SERVICE_AUTH_VERSION,
            "errors": ["service authentication status could not be built"],
            "error_type": type(exc).__name__,
        }


def serialize_service_principal(
    principal: Any,
    *,
    include_private: bool = False,
) -> dict[str, Any]:
    if isinstance(principal, ServicePrincipal):
        return principal.to_dict(include_private=include_private)
    return {}


def serialize_service_auth_result(
    result: Any,
    *,
    include_private: bool = False,
) -> dict[str, Any]:
    if isinstance(result, ServiceAuthResult):
        return result.to_dict(include_private=include_private)
    return {}


__all__ = [
    "SERVICE_AUTH_VERSION",
    "SERVICE_PRINCIPAL_SCHEMA_VERSION",
    "SERVICE_AUTH_RESULT_SCHEMA_VERSION",
    "REQUEST_CONTEXT_SCHEMA_VERSION",
    "DEFAULT_SERVICE_ID_HEADERS",
    "DEFAULT_CREDENTIAL_HEADERS",
    "DEFAULT_EXEMPT_PATHS",
    "DEFAULT_ALLOWED_SERVICE_IDS",
    "DEFAULT_SERVICE_ID_HEADER",
    "DEFAULT_CREDENTIAL_HEADER",
    "DEFAULT_REQUEST_ID_HEADER",
    "DEFAULT_CORRELATION_ID_HEADER",
    "DEFAULT_IDEMPOTENCY_KEY_HEADER",
    "AUTH_CODE_OK",
    "AUTH_CODE_EXEMPT",
    "AUTH_CODE_DISABLED",
    "AUTH_CODE_MISSING_SERVICE_ID",
    "AUTH_CODE_CONFLICTING_SERVICE_ID",
    "AUTH_CODE_INVALID_SERVICE_ID",
    "AUTH_CODE_SERVICE_NOT_ALLOWED",
    "AUTH_CODE_MISSING_CREDENTIAL",
    "AUTH_CODE_CONFLICTING_CREDENTIAL",
    "AUTH_CODE_INVALID_CREDENTIAL",
    "AUTH_CODE_CREDENTIAL_TOO_LONG",
    "AUTH_CODE_UNSUPPORTED_SCHEME",
    "AUTH_CODE_AUTH_NOT_CONFIGURED",
    "AUTH_CODE_REQUEST_INVALID",
    "AUTH_CODE_INTERNAL_ERROR",
    "ServiceAuthenticationError",
    "RequestCorrelationContext",
    "ServicePrincipal",
    "ServiceAuthResult",
    "normalize_service_id",
    "is_service_auth_exempt_path",
    "redact_diagnostic_text",
    "sanitize_diagnostic_value",
    "sanitize_diagnostic_mapping",
    "build_request_correlation_context",
    "authenticate_service_headers",
    "authenticate_service_request",
    "store_service_auth_result",
    "get_current_service_auth_result",
    "get_current_service_principal",
    "get_current_request_correlation",
    "require_authenticated_service",
    "build_service_auth_error_response",
    "apply_correlation_response_headers",
    "service_auth_required",
    "install_service_auth",
    "get_service_auth_status",
    "serialize_service_principal",
    "serialize_service_auth_result",
]
