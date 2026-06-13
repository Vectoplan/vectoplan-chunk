# services/vectoplan-chunk/src/bootstrap/db_bootstrap.py
"""
Explicit database bootstrap orchestrator for the `vectoplan-chunk` service.

This module coordinates the controlled DB bootstrap path.

Responsibilities:
- build effective bootstrap settings
- run schema bootstrap when explicitly enabled
- run default seed bootstrap when explicitly enabled
- ensure schema bootstrap runs before seed bootstrap
- prevent seed bootstrap after failed schema bootstrap
- collect read-only pre/post status
- cleanup SQLAlchemy sessions after each phase
- return a serializable aggregate result for scripts/logs/status output

Important boundaries:
- no Flask app creation here
- no Gunicorn startup integration here
- no request handling here
- no chunk generation here
- no command execution here
- no Snapshot/Event/Command/ObjectRef traversal here
- no Alembic migration execution here

Design rule:

    Normal runtime startup must not call this module automatically.
    This module is intended for an explicit init command/container.

Typical call site:

    app = create_app()
    result = run_db_bootstrap(app)

Later, when Alembic is introduced, schema_bootstrap can be replaced or extended
without changing normal runtime startup behavior.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Final, Mapping, Sequence

try:
    from flask import Flask
except Exception:  # pragma: no cover - partial import environment
    Flask = Any  # type: ignore[misc, assignment]

try:
    from extensions import db as default_db
except Exception:  # pragma: no cover - partial import environment
    default_db = None  # type: ignore[assignment]

try:
    from .db_locks import build_lock_diagnostics, safe_session_cleanup
except Exception:  # pragma: no cover - fallback for direct import tests
    build_lock_diagnostics = None  # type: ignore[assignment]
    safe_session_cleanup = None  # type: ignore[assignment]

try:
    from .default_seed import (
        build_default_seed_status,
        build_default_seed_summary,
        default_seed_result_to_dict,
        run_default_seed,
    )
except Exception:  # pragma: no cover - fallback for partial import tests
    build_default_seed_status = None  # type: ignore[assignment]
    build_default_seed_summary = None  # type: ignore[assignment]
    default_seed_result_to_dict = None  # type: ignore[assignment]
    run_default_seed = None  # type: ignore[assignment]

try:
    from .schema_bootstrap import (
        build_schema_bootstrap_summary,
        build_schema_status,
        run_schema_bootstrap,
        schema_bootstrap_result_to_dict,
    )
except Exception:  # pragma: no cover - fallback for partial import tests
    build_schema_bootstrap_summary = None  # type: ignore[assignment]
    build_schema_status = None  # type: ignore[assignment]
    run_schema_bootstrap = None  # type: ignore[assignment]
    schema_bootstrap_result_to_dict = None  # type: ignore[assignment]

try:
    from .settings import (
        BootstrapSettings,
        build_bootstrap_settings,
        get_bool_setting,
    )
except Exception:  # pragma: no cover - fallback for direct import tests
    BootstrapSettings = Any  # type: ignore[misc, assignment]

    def build_bootstrap_settings(app: Any = None) -> Any:  # type: ignore[override]
        return None

    def get_bool_setting(
        app: Any,
        key: str,
        default: bool = False,
        aliases: Sequence[str] | None = None,
        prefer_env: bool = True,
    ) -> bool:
        try:
            value = getattr(app, "config", {}).get(key, default)
        except Exception:
            value = default

        if isinstance(value, bool):
            return value

        text_value = str(value).strip().lower()
        if text_value in {"1", "true", "yes", "on"}:
            return True
        if text_value in {"0", "false", "no", "off"}:
            return False
        return default


# -----------------------------------------------------------------------------
# Constants
# -----------------------------------------------------------------------------

DB_BOOTSTRAP_RESULT_VERSION: Final[str] = "db-bootstrap-result.v1"

STATUS_COMPLETED: Final[str] = "completed"
STATUS_SKIPPED: Final[str] = "skipped"
STATUS_FAILED: Final[str] = "failed"
STATUS_PARTIAL: Final[str] = "partial"

STEP_STATUS_OK: Final[str] = "ok"
STEP_STATUS_SKIPPED: Final[str] = "skipped"
STEP_STATUS_FAILED: Final[str] = "failed"
STEP_STATUS_WARNING: Final[str] = "warning"

STEP_SCHEMA_STATUS_BEFORE: Final[str] = "schema_status_before"
STEP_SCHEMA_BOOTSTRAP: Final[str] = "schema_bootstrap"
STEP_SEED_STATUS_BEFORE: Final[str] = "seed_status_before"
STEP_DEFAULT_SEED: Final[str] = "default_seed"
STEP_SCHEMA_STATUS_AFTER: Final[str] = "schema_status_after"
STEP_SEED_STATUS_AFTER: Final[str] = "seed_status_after"


# -----------------------------------------------------------------------------
# Data structures
# -----------------------------------------------------------------------------

@dataclass(slots=True)
class DbBootstrapMessage:
    """Serializable DB bootstrap warning/error."""

    code: str
    message: str
    timestamp: str
    details: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class DbBootstrapStep:
    """Serializable DB bootstrap step."""

    name: str
    ok: bool
    status: str
    skipped: bool = False
    message: str | None = None
    started_at: str | None = None
    completed_at: str | None = None
    duration_ms: int = 0
    data: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class DbBootstrapResult:
    """Serializable aggregate DB bootstrap result."""

    ok: bool
    status: str
    result_version: str = DB_BOOTSTRAP_RESULT_VERSION

    started_at: str | None = None
    completed_at: str | None = None
    duration_ms: int = 0

    enabled: bool = False
    schema_bootstrap_requested: bool = False
    seed_bootstrap_requested: bool = False

    schema_bootstrap_executed: bool = False
    seed_bootstrap_executed: bool = False

    schema_bootstrap_ok: bool | None = None
    seed_bootstrap_ok: bool | None = None

    fail_on_error: bool = True

    steps: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[dict[str, Any]] = field(default_factory=list)
    errors: list[dict[str, Any]] = field(default_factory=list)

    schema: dict[str, Any] = field(default_factory=dict)
    seed: dict[str, Any] = field(default_factory=dict)
    pre_status: dict[str, Any] = field(default_factory=dict)
    post_status: dict[str, Any] = field(default_factory=dict)

    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Return serializable dict."""
        return asdict(self)


# -----------------------------------------------------------------------------
# Primitive helpers
# -----------------------------------------------------------------------------

def _utc_now() -> datetime:
    """Return current UTC datetime robustly."""
    try:
        return datetime.now(timezone.utc)
    except Exception:
        return datetime.fromtimestamp(0, tz=timezone.utc)


def _utc_now_iso() -> str:
    """Return UTC timestamp as ISO string."""
    try:
        return _utc_now().isoformat()
    except Exception:
        return "1970-01-01T00:00:00+00:00"


def _duration_ms(started_at_iso: str | None, completed_at_iso: str | None) -> int:
    """Return duration in milliseconds from ISO timestamps."""
    if not started_at_iso or not completed_at_iso:
        return 0

    try:
        started = datetime.fromisoformat(started_at_iso)
        completed = datetime.fromisoformat(completed_at_iso)
        return max(0, int((completed - started).total_seconds() * 1000))
    except Exception:
        return 0


def _safe_str(value: Any, default: str = "") -> str:
    """Normalize value as stripped string."""
    if value is None:
        return default

    try:
        result = str(value).strip()
    except Exception:
        return default

    return result or default


def _safe_bool(value: Any, default: bool = False) -> bool:
    """Normalize value as bool."""
    if isinstance(value, bool):
        return value

    if isinstance(value, int):
        return bool(value)

    text_value = _safe_str(value, "").lower()

    if text_value in {"1", "true", "t", "yes", "y", "on", "enabled"}:
        return True

    if text_value in {"0", "false", "f", "no", "n", "off", "disabled"}:
        return False

    return default


def _safe_exception_message(exc: BaseException | Any) -> str:
    """Return robust exception message."""
    try:
        message = str(exc)
    except Exception:
        message = exc.__class__.__name__

    return message or exc.__class__.__name__


def _safe_dict(value: Any) -> dict[str, Any]:
    """Normalize mapping-like value to dict."""
    if isinstance(value, dict):
        return value

    if isinstance(value, Mapping):
        try:
            return dict(value)
        except Exception:
            return {}

    return {}


def _safe_log_info(app: Any, message: str, *args: Any) -> None:
    """Info-log defensively."""
    try:
        app.logger.info(message, *args)
    except Exception:
        pass


def _safe_log_warning(app: Any, message: str, *args: Any) -> None:
    """Warning-log defensively."""
    try:
        app.logger.warning(message, *args)
    except Exception:
        pass


def _safe_log_exception(app: Any, message: str, *args: Any) -> None:
    """Exception-log defensively."""
    try:
        app.logger.exception(message, *args)
    except Exception:
        pass


def _make_message(
    code: str,
    message: str,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Create serializable bootstrap message."""
    return asdict(
        DbBootstrapMessage(
            code=_safe_str(code, "db_bootstrap_message"),
            message=_safe_str(message, ""),
            timestamp=_utc_now_iso(),
            details=details or {},
        )
    )


def _make_step(
    name: str,
    ok: bool,
    status: str,
    *,
    skipped: bool = False,
    message: str | None = None,
    started_at: str | None = None,
    completed_at: str | None = None,
    data: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Create serializable bootstrap step."""
    started_at = started_at or _utc_now_iso()
    completed_at = completed_at or _utc_now_iso()

    return asdict(
        DbBootstrapStep(
            name=_safe_str(name, "step"),
            ok=bool(ok),
            status=_safe_str(status, STEP_STATUS_FAILED),
            skipped=bool(skipped),
            message=message,
            started_at=started_at,
            completed_at=completed_at,
            duration_ms=_duration_ms(started_at, completed_at),
            data=data or {},
        )
    )


# -----------------------------------------------------------------------------
# App / DB helpers
# -----------------------------------------------------------------------------

def _is_flask_app(app: object) -> bool:
    """Return whether object is Flask-like."""
    if isinstance(app, Flask):
        return True

    required_attrs = ("extensions", "config", "logger")
    try:
        return all(hasattr(app, attr_name) for attr_name in required_attrs)
    except Exception:
        return False


def _cleanup_db_session(
    *,
    rollback: bool = False,
    db_extension: Any = None,
) -> dict[str, bool]:
    """Cleanup SQLAlchemy session robustly."""
    if safe_session_cleanup is not None:
        try:
            return safe_session_cleanup(
                rollback=rollback,
                remove=True,
                db_extension=db_extension,
            )
        except Exception:
            pass

    db_obj = db_extension if db_extension is not None else default_db
    result = {
        "rollback": False,
        "remove": False,
    }

    if db_obj is None:
        return result

    if rollback:
        try:
            db_obj.session.rollback()
            result["rollback"] = True
        except Exception:
            result["rollback"] = False

    try:
        db_obj.session.remove()
        result["remove"] = True
    except Exception:
        result["remove"] = False

    return result


# -----------------------------------------------------------------------------
# Settings resolution
# -----------------------------------------------------------------------------

def resolve_bootstrap_settings(app: Any = None, settings: BootstrapSettings | None = None) -> Any:
    """Resolve aggregate bootstrap settings."""
    if settings is not None:
        return settings

    try:
        resolved = build_bootstrap_settings(app)
        if resolved is not None:
            return resolved
    except Exception:
        pass

    class FallbackSettings:
        class Schema:
            bootstrap_enabled = get_bool_setting(
                app,
                "VECTOPLAN_CHUNK_DB_BOOTSTRAP_ENABLED",
                False,
                aliases=("DB_BOOTSTRAP_ENABLED",),
            )
            create_all = get_bool_setting(
                app,
                "VECTOPLAN_CHUNK_DB_BOOTSTRAP_CREATE_ALL",
                bootstrap_enabled,
                aliases=("DB_BOOTSTRAP_CREATE_ALL",),
            )
            fail_on_error = get_bool_setting(
                app,
                "VECTOPLAN_CHUNK_DB_BOOTSTRAP_FAIL_ON_ERROR",
                True,
                aliases=("DB_BOOTSTRAP_FAIL_ON_ERROR",),
            )

        class Seed:
            seed_defaults = get_bool_setting(
                app,
                "VECTOPLAN_CHUNK_DB_BOOTSTRAP_SEED_DEFAULTS",
                Schema.bootstrap_enabled,
                aliases=("DB_BOOTSTRAP_SEED_DEFAULTS",),
            )
            seed_debug_blocks = get_bool_setting(
                app,
                "VECTOPLAN_CHUNK_DB_BOOTSTRAP_SEED_DEBUG_BLOCKS",
                seed_defaults,
                aliases=("DB_BOOTSTRAP_SEED_DEBUG_BLOCKS",),
            )
            seed_dev_project = get_bool_setting(
                app,
                "VECTOPLAN_CHUNK_DB_BOOTSTRAP_SEED_DEV_PROJECT",
                seed_defaults,
                aliases=("DB_BOOTSTRAP_SEED_DEV_PROJECT",),
            )
            fail_on_error = get_bool_setting(
                app,
                "VECTOPLAN_CHUNK_DB_BOOTSTRAP_FAIL_ON_ERROR",
                True,
                aliases=("DB_BOOTSTRAP_FAIL_ON_ERROR",),
            )

        schema = Schema()
        seed = Seed()

    return FallbackSettings()


def get_effective_db_bootstrap_flags(
    app: Any = None,
    *,
    settings: BootstrapSettings | None = None,
    enabled: bool | None = None,
    run_schema: bool | None = None,
    run_seed: bool | None = None,
    fail_on_error: bool | None = None,
) -> dict[str, bool]:
    """Resolve effective DB bootstrap flags."""
    resolved = resolve_bootstrap_settings(app, settings)

    schema_settings = getattr(resolved, "schema", None)
    seed_settings = getattr(resolved, "seed", None)

    schema_enabled = _safe_bool(
        getattr(schema_settings, "bootstrap_enabled", False),
        False,
    )
    schema_create_all = _safe_bool(
        getattr(schema_settings, "create_all", False),
        False,
    )
    seed_defaults = _safe_bool(
        getattr(seed_settings, "seed_defaults", False),
        False,
    )

    resolved_enabled = bool(
        enabled
        if enabled is not None
        else schema_enabled
    )

    resolved_run_schema = bool(
        run_schema
        if run_schema is not None
        else (resolved_enabled and schema_create_all)
    )

    resolved_run_seed = bool(
        run_seed
        if run_seed is not None
        else (resolved_enabled and seed_defaults)
    )

    resolved_fail_on_error = bool(
        fail_on_error
        if fail_on_error is not None
        else (
            _safe_bool(getattr(schema_settings, "fail_on_error", True), True)
            and _safe_bool(getattr(seed_settings, "fail_on_error", True), True)
        )
    )

    return {
        "enabled": resolved_enabled,
        "runSchema": resolved_run_schema,
        "runSeed": resolved_run_seed,
        "failOnError": resolved_fail_on_error,
    }


# -----------------------------------------------------------------------------
# Status helpers
# -----------------------------------------------------------------------------

def build_db_bootstrap_status(
    app: Flask,
    *,
    db_extension: Any = None,
) -> dict[str, Any]:
    """
    Build read-only DB bootstrap status.

    This does not create tables and does not seed data.
    """
    started_at = _utc_now_iso()

    schema_status: dict[str, Any] = {}
    seed_status: dict[str, Any] = {}

    schema_ok = False
    seed_ok = False

    if build_schema_status is not None:
        try:
            schema_status = build_schema_status(
                app,
                db_extension=db_extension,
            )
            schema_ok = bool(schema_status.get("ok"))
        except Exception as exc:
            schema_status = {
                "ok": False,
                "status": STATUS_FAILED,
                "error": _safe_exception_message(exc),
                "exceptionType": exc.__class__.__name__,
            }
    else:
        schema_status = {
            "ok": False,
            "status": STATUS_FAILED,
            "error": "build_schema_status is unavailable.",
        }

    if build_default_seed_status is not None:
        try:
            seed_status = build_default_seed_status(
                app,
                db_extension=db_extension,
            )
            seed_ok = bool(seed_status.get("ok"))
        except Exception as exc:
            seed_status = {
                "ok": False,
                "status": STATUS_FAILED,
                "error": _safe_exception_message(exc),
                "exceptionType": exc.__class__.__name__,
            }
    else:
        seed_status = {
            "ok": False,
            "status": STATUS_FAILED,
            "error": "build_default_seed_status is unavailable.",
        }

    completed_at = _utc_now_iso()

    return {
        "ok": bool(schema_ok and seed_ok),
        "status": STATUS_COMPLETED if schema_ok and seed_ok else STATUS_PARTIAL,
        "startedAt": started_at,
        "completedAt": completed_at,
        "durationMs": _duration_ms(started_at, completed_at),
        "schema": schema_status,
        "seed": seed_status,
    }


# -----------------------------------------------------------------------------
# Bootstrap runner
# -----------------------------------------------------------------------------

def run_db_bootstrap(
    app: Flask,
    *,
    settings: BootstrapSettings | None = None,
    db_extension: Any = None,
    enabled: bool | None = None,
    run_schema: bool | None = None,
    run_seed: bool | None = None,
    fail_on_error: bool | None = None,
    include_pre_status: bool = True,
    include_post_status: bool = True,
) -> DbBootstrapResult:
    """
    Run explicit DB bootstrap.

    Order:
        1. optional read-only pre-status
        2. schema bootstrap
        3. seed bootstrap
        4. optional read-only post-status

    If schema bootstrap is requested and fails, seed bootstrap is skipped.
    """
    started_at = _utc_now_iso()

    result = DbBootstrapResult(
        ok=False,
        status=STATUS_FAILED,
        started_at=started_at,
    )

    if not _is_flask_app(app):
        result.errors.append(
            _make_message(
                code="invalid_flask_app",
                message="run_db_bootstrap(app) expects a Flask app or compatible object.",
            )
        )
        return _finish_result(result)

    resolved_settings = resolve_bootstrap_settings(app, settings)
    flags = get_effective_db_bootstrap_flags(
        app,
        settings=resolved_settings,
        enabled=enabled,
        run_schema=run_schema,
        run_seed=run_seed,
        fail_on_error=fail_on_error,
    )

    result.enabled = bool(flags["enabled"])
    result.schema_bootstrap_requested = bool(flags["runSchema"])
    result.seed_bootstrap_requested = bool(flags["runSeed"])
    result.fail_on_error = bool(flags["failOnError"])

    try:
        result.metadata["settingsAvailable"] = resolved_settings is not None
        result.metadata["flags"] = flags

        schema_settings = getattr(resolved_settings, "schema", None)
        seed_settings = getattr(resolved_settings, "seed", None)
        identity_settings = getattr(resolved_settings, "identity", None)

        result.metadata["schemaSettings"] = {
            "bootstrapEnabled": _safe_bool(getattr(schema_settings, "bootstrap_enabled", False), False),
            "createAll": _safe_bool(getattr(schema_settings, "create_all", False), False),
            "failOnError": _safe_bool(getattr(schema_settings, "fail_on_error", True), True),
        }
        result.metadata["seedSettings"] = {
            "seedDefaults": _safe_bool(getattr(seed_settings, "seed_defaults", False), False),
            "seedDebugBlocks": _safe_bool(getattr(seed_settings, "seed_debug_blocks", False), False),
            "seedDevProject": _safe_bool(getattr(seed_settings, "seed_dev_project", False), False),
            "seedOnEmptyOnly": _safe_bool(getattr(seed_settings, "seed_on_empty_only", True), True),
            "failOnError": _safe_bool(getattr(seed_settings, "fail_on_error", True), True),
        }
        result.metadata["identity"] = {
            "mode": _safe_str(getattr(identity_settings, "mode", ""), ""),
            "isRuntimeMode": _safe_bool(getattr(identity_settings, "is_runtime_mode", False), False),
            "isDbBootstrapMode": _safe_bool(getattr(identity_settings, "is_db_bootstrap_mode", False), False),
        }

        if build_lock_diagnostics is not None:
            result.metadata["lockDiagnostics"] = build_lock_diagnostics(app, db_extension)
    except Exception:
        pass

    if not result.enabled:
        result.steps.append(
            _make_step(
                name="db_bootstrap",
                ok=True,
                status=STEP_STATUS_SKIPPED,
                skipped=True,
                message="DB bootstrap disabled by settings.",
            )
        )
        result.ok = True
        result.status = STATUS_SKIPPED
        return _finish_result(result)

    if not result.schema_bootstrap_requested and not result.seed_bootstrap_requested:
        result.steps.append(
            _make_step(
                name="db_bootstrap",
                ok=True,
                status=STEP_STATUS_SKIPPED,
                skipped=True,
                message="DB bootstrap enabled, but no bootstrap phase is requested.",
            )
        )
        result.ok = True
        result.status = STATUS_SKIPPED
        return _finish_result(result)

    _safe_log_info(
        app,
        "DB bootstrap started. run_schema=%s run_seed=%s",
        result.schema_bootstrap_requested,
        result.seed_bootstrap_requested,
    )

    if include_pre_status:
        _run_pre_status_step(app, result, db_extension=db_extension)

    # Phase 1: schema bootstrap.
    if result.schema_bootstrap_requested:
        _run_schema_step(
            app,
            result,
            resolved_settings=resolved_settings,
            db_extension=db_extension,
        )

        if result.schema_bootstrap_ok is False:
            if result.seed_bootstrap_requested:
                result.steps.append(
                    _make_step(
                        name=STEP_DEFAULT_SEED,
                        ok=True,
                        status=STEP_STATUS_SKIPPED,
                        skipped=True,
                        message="Default seed skipped because schema bootstrap failed.",
                    )
                )

            _cleanup_db_session(rollback=True, db_extension=db_extension)
            return _finish_or_raise(app, result, result.fail_on_error)
    else:
        result.steps.append(
            _make_step(
                name=STEP_SCHEMA_BOOTSTRAP,
                ok=True,
                status=STEP_STATUS_SKIPPED,
                skipped=True,
                message="Schema bootstrap not requested.",
            )
        )
        result.schema_bootstrap_ok = None

    # Phase 2: default seed.
    if result.seed_bootstrap_requested:
        _run_seed_step(
            app,
            result,
            resolved_settings=resolved_settings,
            db_extension=db_extension,
        )

        if result.seed_bootstrap_ok is False:
            _cleanup_db_session(rollback=True, db_extension=db_extension)
            return _finish_or_raise(app, result, result.fail_on_error)
    else:
        result.steps.append(
            _make_step(
                name=STEP_DEFAULT_SEED,
                ok=True,
                status=STEP_STATUS_SKIPPED,
                skipped=True,
                message="Default seed not requested.",
            )
        )
        result.seed_bootstrap_ok = None

    if include_post_status:
        _run_post_status_step(app, result, db_extension=db_extension)

    if result.errors:
        return _finish_or_raise(app, result, result.fail_on_error)

    result.ok = True
    result.status = STATUS_COMPLETED

    _safe_log_info(
        app,
        "DB bootstrap completed successfully. schema_ok=%s seed_ok=%s",
        result.schema_bootstrap_ok,
        result.seed_bootstrap_ok,
    )

    return _finish_result(result)


def _run_pre_status_step(
    app: Flask,
    result: DbBootstrapResult,
    *,
    db_extension: Any = None,
) -> None:
    """Run read-only pre-status step."""
    started_at = _utc_now_iso()

    try:
        status = build_db_bootstrap_status(app, db_extension=db_extension)
        result.pre_status = status
        result.steps.append(
            _make_step(
                name="pre_status",
                ok=True,
                status=STEP_STATUS_OK,
                message="Read-only pre-bootstrap status collected.",
                started_at=started_at,
                data={
                    "ok": bool(status.get("ok")),
                    "schemaOk": bool((status.get("schema") or {}).get("ok")),
                    "seedOk": bool((status.get("seed") or {}).get("ok")),
                },
            )
        )
    except Exception as exc:
        message = _safe_exception_message(exc)
        result.warnings.append(
            _make_message(
                code="pre_status_failed",
                message=message,
                details={
                    "exceptionType": exc.__class__.__name__,
                },
            )
        )
        result.steps.append(
            _make_step(
                name="pre_status",
                ok=False,
                status=STEP_STATUS_WARNING,
                message=message,
                started_at=started_at,
                data={
                    "exceptionType": exc.__class__.__name__,
                },
            )
        )


def _run_schema_step(
    app: Flask,
    result: DbBootstrapResult,
    *,
    resolved_settings: Any,
    db_extension: Any = None,
) -> None:
    """Run schema bootstrap step."""
    started_at = _utc_now_iso()

    if run_schema_bootstrap is None:
        message = "run_schema_bootstrap is unavailable."
        result.schema_bootstrap_ok = False
        result.errors.append(
            _make_message(
                code="schema_bootstrap_unavailable",
                message=message,
            )
        )
        result.steps.append(
            _make_step(
                name=STEP_SCHEMA_BOOTSTRAP,
                ok=False,
                status=STEP_STATUS_FAILED,
                message=message,
                started_at=started_at,
            )
        )
        return

    schema_settings = getattr(resolved_settings, "schema", None)

    try:
        schema_result = run_schema_bootstrap(
            app,
            settings=schema_settings,
            db_extension=db_extension,
            fail_on_error=False,
        )

        if schema_bootstrap_result_to_dict is not None:
            schema_data = schema_bootstrap_result_to_dict(schema_result)
        else:
            schema_data = _safe_dict(schema_result)

        result.schema = schema_data
        result.schema_bootstrap_executed = True
        result.schema_bootstrap_ok = bool(schema_data.get("ok"))

        summary = {}
        if build_schema_bootstrap_summary is not None:
            try:
                summary = build_schema_bootstrap_summary(schema_result)
            except Exception:
                summary = {}

        result.steps.append(
            _make_step(
                name=STEP_SCHEMA_BOOTSTRAP,
                ok=bool(schema_data.get("ok")),
                status=STEP_STATUS_OK if schema_data.get("ok") else STEP_STATUS_FAILED,
                message=(
                    "Schema bootstrap completed."
                    if schema_data.get("ok")
                    else "Schema bootstrap failed."
                ),
                started_at=started_at,
                data={
                    "summary": summary,
                    "result": schema_data,
                },
            )
        )

        if not schema_data.get("ok"):
            result.errors.append(
                _make_message(
                    code="schema_bootstrap_failed",
                    message="Schema bootstrap failed.",
                    details=summary or schema_data,
                )
            )

    except Exception as exc:
        message = _safe_exception_message(exc)
        result.schema_bootstrap_executed = True
        result.schema_bootstrap_ok = False
        result.errors.append(
            _make_message(
                code="schema_bootstrap_exception",
                message=message,
                details={
                    "exceptionType": exc.__class__.__name__,
                },
            )
        )
        result.steps.append(
            _make_step(
                name=STEP_SCHEMA_BOOTSTRAP,
                ok=False,
                status=STEP_STATUS_FAILED,
                message=message,
                started_at=started_at,
                data={
                    "exceptionType": exc.__class__.__name__,
                },
            )
        )


def _run_seed_step(
    app: Flask,
    result: DbBootstrapResult,
    *,
    resolved_settings: Any,
    db_extension: Any = None,
) -> None:
    """Run default seed bootstrap step."""
    started_at = _utc_now_iso()

    if run_default_seed is None:
        message = "run_default_seed is unavailable."
        result.seed_bootstrap_ok = False
        result.errors.append(
            _make_message(
                code="default_seed_unavailable",
                message=message,
            )
        )
        result.steps.append(
            _make_step(
                name=STEP_DEFAULT_SEED,
                ok=False,
                status=STEP_STATUS_FAILED,
                message=message,
                started_at=started_at,
            )
        )
        return

    seed_settings = getattr(resolved_settings, "seed", None)
    world_defaults = getattr(resolved_settings, "world_defaults", None)
    block_defaults = getattr(resolved_settings, "block_defaults", None)

    try:
        seed_result = run_default_seed(
            app,
            seed_settings=seed_settings,
            world_defaults=world_defaults,
            block_defaults=block_defaults,
            db_extension=db_extension,
            fail_on_error=False,
        )

        if default_seed_result_to_dict is not None:
            seed_data = default_seed_result_to_dict(seed_result)
        else:
            seed_data = _safe_dict(seed_result)

        result.seed = seed_data
        result.seed_bootstrap_executed = True
        result.seed_bootstrap_ok = bool(seed_data.get("ok"))

        summary = {}
        if build_default_seed_summary is not None:
            try:
                summary = build_default_seed_summary(seed_result)
            except Exception:
                summary = {}

        result.steps.append(
            _make_step(
                name=STEP_DEFAULT_SEED,
                ok=bool(seed_data.get("ok")),
                status=STEP_STATUS_OK if seed_data.get("ok") else STEP_STATUS_FAILED,
                message=(
                    "Default seed bootstrap completed."
                    if seed_data.get("ok")
                    else "Default seed bootstrap failed."
                ),
                started_at=started_at,
                data={
                    "summary": summary,
                    "result": seed_data,
                },
            )
        )

        if not seed_data.get("ok"):
            result.errors.append(
                _make_message(
                    code="default_seed_failed",
                    message="Default seed bootstrap failed.",
                    details=summary or seed_data,
                )
            )

    except Exception as exc:
        message = _safe_exception_message(exc)
        result.seed_bootstrap_executed = True
        result.seed_bootstrap_ok = False
        result.errors.append(
            _make_message(
                code="default_seed_exception",
                message=message,
                details={
                    "exceptionType": exc.__class__.__name__,
                },
            )
        )
        result.steps.append(
            _make_step(
                name=STEP_DEFAULT_SEED,
                ok=False,
                status=STEP_STATUS_FAILED,
                message=message,
                started_at=started_at,
                data={
                    "exceptionType": exc.__class__.__name__,
                },
            )
        )


def _run_post_status_step(
    app: Flask,
    result: DbBootstrapResult,
    *,
    db_extension: Any = None,
) -> None:
    """Run read-only post-status step."""
    started_at = _utc_now_iso()

    try:
        status = build_db_bootstrap_status(app, db_extension=db_extension)
        result.post_status = status
        result.steps.append(
            _make_step(
                name="post_status",
                ok=True,
                status=STEP_STATUS_OK,
                message="Read-only post-bootstrap status collected.",
                started_at=started_at,
                data={
                    "ok": bool(status.get("ok")),
                    "schemaOk": bool((status.get("schema") or {}).get("ok")),
                    "seedOk": bool((status.get("seed") or {}).get("ok")),
                },
            )
        )
    except Exception as exc:
        message = _safe_exception_message(exc)
        result.warnings.append(
            _make_message(
                code="post_status_failed",
                message=message,
                details={
                    "exceptionType": exc.__class__.__name__,
                },
            )
        )
        result.steps.append(
            _make_step(
                name="post_status",
                ok=False,
                status=STEP_STATUS_WARNING,
                message=message,
                started_at=started_at,
                data={
                    "exceptionType": exc.__class__.__name__,
                },
            )
        )


def _finish_result(result: DbBootstrapResult) -> DbBootstrapResult:
    """Finalize result timestamps/status."""
    result.completed_at = _utc_now_iso()
    result.duration_ms = _duration_ms(result.started_at, result.completed_at)

    if result.errors:
        result.ok = False
        result.status = STATUS_FAILED
    elif result.status not in {STATUS_SKIPPED, STATUS_PARTIAL}:
        result.ok = True
        result.status = STATUS_COMPLETED

    _cleanup_db_session(rollback=False)

    return result


def _finish_or_raise(
    app: Any,
    result: DbBootstrapResult,
    fail_on_error: bool,
) -> DbBootstrapResult:
    """Finish result and optionally raise."""
    _finish_result(result)

    if fail_on_error and not result.ok:
        first_error = result.errors[0] if result.errors else {}
        message = _safe_str(
            first_error.get("message") if isinstance(first_error, Mapping) else None,
            "DB bootstrap failed.",
        )
        _safe_log_exception(app, "DB bootstrap failed: %s", message)
        raise RuntimeError(message)

    if not result.ok:
        _safe_log_warning(app, "DB bootstrap failed but fail_on_error=false.")

    return result


# -----------------------------------------------------------------------------
# Convenience APIs
# -----------------------------------------------------------------------------

def run_db_bootstrap_if_enabled(
    app: Flask,
    *,
    settings: BootstrapSettings | None = None,
    db_extension: Any = None,
) -> DbBootstrapResult:
    """Run DB bootstrap if enabled by settings."""
    return run_db_bootstrap(
        app,
        settings=settings,
        db_extension=db_extension,
    )


def db_bootstrap_result_to_dict(
    result: DbBootstrapResult | Mapping[str, Any] | Any,
) -> dict[str, Any]:
    """Serialize DB bootstrap result to dict."""
    if isinstance(result, DbBootstrapResult):
        return result.to_dict()

    if isinstance(result, Mapping):
        try:
            return dict(result)
        except Exception:
            return {}

    return {}


def build_db_bootstrap_summary(
    result: DbBootstrapResult | Mapping[str, Any] | Any,
) -> dict[str, Any]:
    """Build compact DB bootstrap summary."""
    data = db_bootstrap_result_to_dict(result)
    steps = data.get("steps") or []

    try:
        failed_steps = [step for step in steps if not bool(step.get("ok"))]
        skipped_steps = [step for step in steps if bool(step.get("skipped"))]
    except Exception:
        failed_steps = []
        skipped_steps = []

    return {
        "ok": bool(data.get("ok")),
        "status": _safe_str(data.get("status"), "unknown"),
        "enabled": bool(data.get("enabled")),
        "schemaBootstrapRequested": bool(data.get("schema_bootstrap_requested")),
        "seedBootstrapRequested": bool(data.get("seed_bootstrap_requested")),
        "schemaBootstrapExecuted": bool(data.get("schema_bootstrap_executed")),
        "seedBootstrapExecuted": bool(data.get("seed_bootstrap_executed")),
        "schemaBootstrapOk": data.get("schema_bootstrap_ok"),
        "seedBootstrapOk": data.get("seed_bootstrap_ok"),
        "failOnError": bool(data.get("fail_on_error")),
        "stepCount": len(steps),
        "failedStepCount": len(failed_steps),
        "skippedStepCount": len(skipped_steps),
        "warningCount": len(data.get("warnings") or []),
        "errorCount": len(data.get("errors") or []),
        "durationMs": data.get("duration_ms"),
    }


def build_db_bootstrap_exit_code(
    result: DbBootstrapResult | Mapping[str, Any] | Any,
) -> int:
    """
    Return conventional process exit code for DB bootstrap result.

    0 = ok or skipped
    1 = failed
    """
    data = db_bootstrap_result_to_dict(result)
    return 0 if bool(data.get("ok")) else 1


# -----------------------------------------------------------------------------
# Public exports
# -----------------------------------------------------------------------------

__all__ = [
    "DB_BOOTSTRAP_RESULT_VERSION",
    "STATUS_COMPLETED",
    "STATUS_FAILED",
    "STATUS_PARTIAL",
    "STATUS_SKIPPED",
    "STEP_DEFAULT_SEED",
    "STEP_SCHEMA_BOOTSTRAP",
    "STEP_SCHEMA_STATUS_AFTER",
    "STEP_SCHEMA_STATUS_BEFORE",
    "STEP_SEED_STATUS_AFTER",
    "STEP_SEED_STATUS_BEFORE",
    "STEP_STATUS_FAILED",
    "STEP_STATUS_OK",
    "STEP_STATUS_SKIPPED",
    "STEP_STATUS_WARNING",
    "DbBootstrapMessage",
    "DbBootstrapResult",
    "DbBootstrapStep",
    "build_db_bootstrap_exit_code",
    "build_db_bootstrap_status",
    "build_db_bootstrap_summary",
    "db_bootstrap_result_to_dict",
    "get_effective_db_bootstrap_flags",
    "resolve_bootstrap_settings",
    "run_db_bootstrap",
    "run_db_bootstrap_if_enabled",
]