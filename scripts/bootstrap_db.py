# services/vectoplan-chunk/scripts/bootstrap_db.py
"""
Explicit database bootstrap command for the `vectoplan-chunk` service.

This script is the controlled entrypoint for local/dev DB initialization.

Responsibilities:
- create a Flask app safely
- explicitly disable normal runtime startup hooks while the app is being created
- run schema bootstrap when requested
- run default seed bootstrap when requested
- print a JSON or human-readable result
- return a useful process exit code

Important boundaries:
- this script is not the normal Gunicorn runtime
- this script should not serve HTTP requests
- this script should not generate chunks
- this script should not execute chunk commands
- this script should not load snapshots/events/object refs
- this script should not replace Alembic in production

Typical local usage from service root:

    python scripts/bootstrap_db.py --create-all --seed --json

Typical container usage:

    python /opt/vectoplan/services/vectoplan-chunk/scripts/bootstrap_db.py --create-all --seed --json

Exit codes:
    0 = bootstrap succeeded or was intentionally skipped
    1 = bootstrap failed
    2 = app creation/import failed
    3 = invalid arguments
"""

from __future__ import annotations

import argparse
import importlib
import json
import os
import sys
import traceback
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence


# -----------------------------------------------------------------------------
# Constants
# -----------------------------------------------------------------------------

SCRIPT_RESULT_VERSION = "bootstrap-db-script-result.v1"

EXIT_OK = 0
EXIT_BOOTSTRAP_FAILED = 1
EXIT_APP_FAILED = 2
EXIT_INVALID_ARGS = 3

DEFAULT_CREATE_ALL = True
DEFAULT_SEED = True
DEFAULT_JSON = False


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
    """Return current UTC timestamp as ISO string."""
    try:
        return _utc_now().isoformat()
    except Exception:
        return "1970-01-01T00:00:00+00:00"


def _duration_ms(started_at: str | None, completed_at: str | None) -> int:
    """Return duration in milliseconds from ISO timestamps."""
    if not started_at or not completed_at:
        return 0

    try:
        started = datetime.fromisoformat(started_at)
        completed = datetime.fromisoformat(completed_at)
        return max(0, int((completed - started).total_seconds() * 1000))
    except Exception:
        return 0


def _safe_str(value: Any, default: str = "") -> str:
    """Convert value to stripped string."""
    if value is None:
        return default

    try:
        result = str(value).strip()
    except Exception:
        return default

    return result or default


def _safe_bool(value: Any, default: bool = False) -> bool:
    """Convert value to bool robustly."""
    if isinstance(value, bool):
        return value

    if isinstance(value, int):
        return bool(value)

    text = _safe_str(value, "").lower()

    if text in {"1", "true", "t", "yes", "y", "on", "enabled"}:
        return True

    if text in {"0", "false", "f", "no", "n", "off", "disabled"}:
        return False

    return default


def _safe_exception_message(exc: BaseException | Any) -> str:
    """Return robust exception message."""
    try:
        message = str(exc)
    except Exception:
        message = exc.__class__.__name__

    return message or exc.__class__.__name__


def _to_plain_dict(value: Any) -> dict[str, Any]:
    """Convert dataclass/mapping/object result to plain dict."""
    if isinstance(value, dict):
        return value

    if isinstance(value, Mapping):
        try:
            return dict(value)
        except Exception:
            return {}

    try:
        if is_dataclass(value):
            return asdict(value)
    except Exception:
        pass

    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        try:
            result = to_dict()
            if isinstance(result, dict):
                return result
        except Exception:
            return {}

    return {}


def _json_default(value: Any) -> Any:
    """JSON serializer fallback."""
    try:
        if is_dataclass(value):
            return asdict(value)
    except Exception:
        pass

    if isinstance(value, Path):
        return str(value)

    if isinstance(value, datetime):
        return value.isoformat()

    try:
        return str(value)
    except Exception:
        return repr(value)


def _print_json(value: Any, *, pretty: bool = True) -> None:
    """Print JSON to stdout."""
    if pretty:
        print(json.dumps(value, indent=2, sort_keys=True, default=_json_default))
    else:
        print(json.dumps(value, separators=(",", ":"), sort_keys=True, default=_json_default))


def _print_human_result(result: dict[str, Any]) -> None:
    """Print compact human-readable result."""
    print("")
    print("VECTOPLAN Chunk DB Bootstrap")
    print("=" * 32)
    print(f"ok:                 {result.get('ok')}")
    print(f"status:             {result.get('status')}")
    print(f"durationMs:         {result.get('durationMs')}")
    print(f"schema requested:   {result.get('schemaBootstrapRequested')}")
    print(f"schema executed:    {result.get('schemaBootstrapExecuted')}")
    print(f"schema ok:          {result.get('schemaBootstrapOk')}")
    print(f"seed requested:     {result.get('seedBootstrapRequested')}")
    print(f"seed executed:      {result.get('seedBootstrapExecuted')}")
    print(f"seed ok:            {result.get('seedBootstrapOk')}")
    print(f"warnings:           {result.get('warningCount')}")
    print(f"errors:             {result.get('errorCount')}")
    print("")

    errors = result.get("errors") or []
    if errors:
        print("Errors:")
        for item in errors:
            message = _safe_str(item.get("message") if isinstance(item, dict) else item, "")
            code = _safe_str(item.get("code") if isinstance(item, dict) else "", "")
            print(f"  - {code}: {message}")
        print("")

    warnings = result.get("warnings") or []
    if warnings:
        print("Warnings:")
        for item in warnings:
            message = _safe_str(item.get("message") if isinstance(item, dict) else item, "")
            code = _safe_str(item.get("code") if isinstance(item, dict) else "", "")
            print(f"  - {code}: {message}")
        print("")


# -----------------------------------------------------------------------------
# Path/bootstrap helpers
# -----------------------------------------------------------------------------

def resolve_service_root() -> Path:
    """
    Resolve service root.

    Expected:
        services/vectoplan-chunk/scripts/bootstrap_db.py

    parents[0] -> scripts
    parents[1] -> vectoplan-chunk
    """
    try:
        return Path(__file__).resolve().parents[1]
    except Exception:
        return Path.cwd()


def configure_python_path(service_root: Path) -> None:
    """Ensure service root is importable."""
    try:
        root_text = str(service_root)
    except Exception:
        root_text = ""

    if root_text and root_text not in sys.path:
        sys.path.insert(0, root_text)


def set_default_env(
    *,
    create_all: bool,
    seed: bool,
    mode: str,
    force_runtime_hooks_off: bool,
) -> None:
    """
    Set safe default env values before app import.

    Existing environment values are respected unless explicitly safety-critical.

    The key safety rule is:
        normal runtime startup hooks must not perform DB mutation while this
        script creates the Flask app. The DB bootstrap happens explicitly after
        app creation.
    """
    os.environ.setdefault("VECTOPLAN_CHUNK_MODE", mode)
    os.environ.setdefault("VECTOPLAN_CHUNK_RUN_MODE", mode)

    os.environ.setdefault("VECTOPLAN_CHUNK_DB_BOOTSTRAP_ENABLED", "true")
    os.environ.setdefault("VECTOPLAN_CHUNK_DB_BOOTSTRAP_CREATE_ALL", "true" if create_all else "false")
    os.environ.setdefault("VECTOPLAN_CHUNK_DB_BOOTSTRAP_SEED_DEFAULTS", "true" if seed else "false")
    os.environ.setdefault("VECTOPLAN_CHUNK_DB_BOOTSTRAP_SEED_DEBUG_BLOCKS", "true" if seed else "false")
    os.environ.setdefault("VECTOPLAN_CHUNK_DB_BOOTSTRAP_SEED_DEV_PROJECT", "true" if seed else "false")
    os.environ.setdefault("VECTOPLAN_CHUNK_SEED_ON_EMPTY_ONLY", "true")
    os.environ.setdefault("VECTOPLAN_CHUNK_DB_BOOTSTRAP_ADVISORY_LOCKS", "true")
    os.environ.setdefault("VECTOPLAN_CHUNK_DB_BOOTSTRAP_FAIL_ON_ERROR", "true")

    os.environ.setdefault("VECTOPLAN_CHUNK_ALLOW_RUNTIME_DB_MUTATIONS", "false")
    os.environ.setdefault("VECTOPLAN_CHUNK_AUTO_CREATE_ALL", "false")
    os.environ.setdefault("VECTOPLAN_CHUNK_AUTO_SEED_DEFAULTS", "false")
    os.environ.setdefault("VECTOPLAN_CHUNK_SEED_DEBUG_BLOCKS", "false")
    os.environ.setdefault("VECTOPLAN_CHUNK_SEED_DEV_PROJECT", "false")

    if force_runtime_hooks_off:
        os.environ["VECTOPLAN_CHUNK_RUN_STARTUP_HOOKS"] = "false"
    else:
        os.environ.setdefault("VECTOPLAN_CHUNK_RUN_STARTUP_HOOKS", "false")


def create_flask_app(
    *,
    app_factory: str,
    config_name: str | None = None,
) -> Any:
    """
    Import and create Flask app.

    app_factory syntax:
        app:create_app
        wsgi:app
        wsgi:application

    If the target is callable, it is called.
    If it is already an app object, it is returned.
    """
    module_name, sep, attr_name = app_factory.partition(":")

    if not sep or not module_name or not attr_name:
        raise ValueError(
            f"Invalid app factory '{app_factory}'. Expected format 'module:attribute'."
        )

    module = importlib.import_module(module_name)
    target = getattr(module, attr_name)

    if callable(target):
        try:
            if config_name:
                return target(config_name)
        except TypeError:
            pass

        try:
            return target()
        except TypeError:
            return target

    return target


# -----------------------------------------------------------------------------
# Result helpers
# -----------------------------------------------------------------------------

def make_script_result(
    *,
    ok: bool,
    status: str,
    started_at: str,
    completed_at: str,
    args: argparse.Namespace,
    bootstrap_result: dict[str, Any] | None = None,
    error: str | None = None,
    traceback_text: str | None = None,
    service_root: Path | None = None,
) -> dict[str, Any]:
    """Build serializable script result."""
    bootstrap_result = bootstrap_result or {}
    summary = bootstrap_result.get("summary") or {}

    result = {
        "ok": bool(ok),
        "status": _safe_str(status, "unknown"),
        "resultVersion": SCRIPT_RESULT_VERSION,
        "startedAt": started_at,
        "completedAt": completed_at,
        "durationMs": _duration_ms(started_at, completed_at),
        "serviceRoot": str(service_root) if service_root is not None else "",
        "appFactory": args.app_factory,
        "configName": args.config,
        "createAll": bool(args.create_all),
        "seed": bool(args.seed),
        "checkOnly": bool(args.check_only),
        "mode": args.mode,
        "bootstrap": bootstrap_result,
        "summary": summary,
        "error": error,
    }

    if traceback_text:
        result["traceback"] = traceback_text

    if bootstrap_result:
        result.update(
            {
                "schemaBootstrapRequested": summary.get("schemaBootstrapRequested"),
                "schemaBootstrapExecuted": summary.get("schemaBootstrapExecuted"),
                "schemaBootstrapOk": summary.get("schemaBootstrapOk"),
                "seedBootstrapRequested": summary.get("seedBootstrapRequested"),
                "seedBootstrapExecuted": summary.get("seedBootstrapExecuted"),
                "seedBootstrapOk": summary.get("seedBootstrapOk"),
                "warningCount": summary.get("warningCount"),
                "errorCount": summary.get("errorCount"),
            }
        )

    return result


def summarize_bootstrap_result(result: Any) -> dict[str, Any]:
    """Build compact summary from db_bootstrap result."""
    try:
        from src.bootstrap.db_bootstrap import build_db_bootstrap_summary

        summary = build_db_bootstrap_summary(result)
        if isinstance(summary, dict):
            return summary
    except Exception:
        pass

    data = _to_plain_dict(result)

    return {
        "ok": bool(data.get("ok")),
        "status": _safe_str(data.get("status"), "unknown"),
        "enabled": bool(data.get("enabled")),
        "schemaBootstrapRequested": bool(data.get("schema_bootstrap_requested")),
        "schemaBootstrapExecuted": bool(data.get("schema_bootstrap_executed")),
        "schemaBootstrapOk": data.get("schema_bootstrap_ok"),
        "seedBootstrapRequested": bool(data.get("seed_bootstrap_requested")),
        "seedBootstrapExecuted": bool(data.get("seed_bootstrap_executed")),
        "seedBootstrapOk": data.get("seed_bootstrap_ok"),
        "warningCount": len(data.get("warnings") or []),
        "errorCount": len(data.get("errors") or []),
        "durationMs": data.get("duration_ms"),
    }


def normalize_bootstrap_result(result: Any) -> dict[str, Any]:
    """Normalize DB bootstrap result to plain dict and attach summary."""
    data = _to_plain_dict(result)
    data["summary"] = summarize_bootstrap_result(result)
    return data


# -----------------------------------------------------------------------------
# CLI
# -----------------------------------------------------------------------------

def build_arg_parser() -> argparse.ArgumentParser:
    """Build argument parser."""
    parser = argparse.ArgumentParser(
        description="Bootstrap the VECTOPLAN Chunk Service database explicitly.",
    )

    parser.add_argument(
        "--app-factory",
        default=os.getenv("VECTOPLAN_CHUNK_BOOTSTRAP_APP_FACTORY", "app:create_app"),
        help="Flask app factory target. Default: app:create_app",
    )
    parser.add_argument(
        "--config",
        default=os.getenv("VECTOPLAN_CHUNK_CONFIG", None),
        help="Optional config name passed to create_app(config).",
    )
    parser.add_argument(
        "--mode",
        default=os.getenv("VECTOPLAN_CHUNK_MODE", "db-bootstrap"),
        help="Bootstrap mode value placed in VECTOPLAN_CHUNK_MODE. Default: db-bootstrap",
    )

    create_group = parser.add_mutually_exclusive_group()
    create_group.add_argument(
        "--create-all",
        dest="create_all",
        action="store_true",
        default=DEFAULT_CREATE_ALL,
        help="Run schema bootstrap using db.create_all(). Default: enabled.",
    )
    create_group.add_argument(
        "--no-create-all",
        dest="create_all",
        action="store_false",
        help="Do not run db.create_all().",
    )

    seed_group = parser.add_mutually_exclusive_group()
    seed_group.add_argument(
        "--seed",
        dest="seed",
        action="store_true",
        default=DEFAULT_SEED,
        help="Run default seed bootstrap. Default: enabled.",
    )
    seed_group.add_argument(
        "--no-seed",
        dest="seed",
        action="store_false",
        help="Do not run default seed bootstrap.",
    )

    parser.add_argument(
        "--check-only",
        action="store_true",
        help="Only build read-only DB bootstrap status. No create_all and no seed.",
    )
    parser.add_argument(
        "--fail-on-error",
        action="store_true",
        default=True,
        help="Return failure exit code on bootstrap errors. Default: true.",
    )
    parser.add_argument(
        "--no-fail-on-error",
        dest="fail_on_error",
        action="store_false",
        help="Do not raise/fail process on bootstrap errors.",
    )

    parser.add_argument(
        "--json",
        action="store_true",
        default=DEFAULT_JSON,
        help="Print full JSON result.",
    )
    parser.add_argument(
        "--compact-json",
        action="store_true",
        help="Print compact JSON result.",
    )
    parser.add_argument(
        "--debug-traceback",
        action="store_true",
        help="Include traceback in JSON result on failures.",
    )
    parser.add_argument(
        "--allow-runtime-startup-hooks",
        action="store_true",
        help=(
            "Do not force VECTOPLAN_CHUNK_RUN_STARTUP_HOOKS=false before app import. "
            "Use only for diagnostics."
        ),
    )

    return parser


# -----------------------------------------------------------------------------
# Main
# -----------------------------------------------------------------------------

def main(argv: Sequence[str] | None = None) -> int:
    """Script entrypoint."""
    parser = build_arg_parser()

    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        try:
            return int(exc.code)
        except Exception:
            return EXIT_INVALID_ARGS

    started_at = _utc_now_iso()
    service_root = resolve_service_root()

    try:
        configure_python_path(service_root)

        effective_create_all = bool(args.create_all)
        effective_seed = bool(args.seed)

        if args.check_only:
            effective_create_all = False
            effective_seed = False

        set_default_env(
            create_all=effective_create_all,
            seed=effective_seed,
            mode=args.mode,
            force_runtime_hooks_off=not bool(args.allow_runtime_startup_hooks),
        )

        app = create_flask_app(
            app_factory=args.app_factory,
            config_name=args.config,
        )

    except Exception as exc:
        completed_at = _utc_now_iso()
        traceback_text = traceback.format_exc() if args.debug_traceback else None

        result = make_script_result(
            ok=False,
            status="app_failed",
            started_at=started_at,
            completed_at=completed_at,
            args=args,
            error=_safe_exception_message(exc),
            traceback_text=traceback_text,
            service_root=service_root,
        )

        if args.json or args.compact_json:
            _print_json(result, pretty=not args.compact_json)
        else:
            _print_human_result(result)
            if traceback_text:
                print(traceback_text)

        return EXIT_APP_FAILED

    try:
        if args.check_only:
            from src.bootstrap.db_bootstrap import build_db_bootstrap_status

            status = build_db_bootstrap_status(app)
            bootstrap_result = {
                "ok": bool(status.get("ok")),
                "status": status.get("status"),
                "enabled": False,
                "schema_bootstrap_requested": False,
                "seed_bootstrap_requested": False,
                "schema_bootstrap_executed": False,
                "seed_bootstrap_executed": False,
                "schema_bootstrap_ok": bool((status.get("schema") or {}).get("ok")),
                "seed_bootstrap_ok": bool((status.get("seed") or {}).get("ok")),
                "warnings": [],
                "errors": [] if status.get("ok") else [
                    {
                        "code": "check_only_not_complete",
                        "message": "DB bootstrap read-only status is not complete.",
                        "details": status,
                    }
                ],
                "pre_status": status,
            }
        else:
            from src.bootstrap.db_bootstrap import run_db_bootstrap

            raw_result = run_db_bootstrap(
                app,
                enabled=True,
                run_schema=effective_create_all,
                run_seed=effective_seed,
                fail_on_error=False,
                include_pre_status=True,
                include_post_status=True,
            )
            bootstrap_result = normalize_bootstrap_result(raw_result)

        if "summary" not in bootstrap_result:
            bootstrap_result["summary"] = summarize_bootstrap_result(bootstrap_result)

        ok = bool(bootstrap_result.get("ok"))
        completed_at = _utc_now_iso()

        script_result = make_script_result(
            ok=ok,
            status="completed" if ok else "failed",
            started_at=started_at,
            completed_at=completed_at,
            args=args,
            bootstrap_result=bootstrap_result,
            service_root=service_root,
        )

        if args.json or args.compact_json:
            _print_json(script_result, pretty=not args.compact_json)
        else:
            _print_human_result(script_result)

        if ok:
            return EXIT_OK

        return EXIT_BOOTSTRAP_FAILED if args.fail_on_error else EXIT_OK

    except Exception as exc:
        completed_at = _utc_now_iso()
        traceback_text = traceback.format_exc() if args.debug_traceback else None

        script_result = make_script_result(
            ok=False,
            status="bootstrap_failed",
            started_at=started_at,
            completed_at=completed_at,
            args=args,
            error=_safe_exception_message(exc),
            traceback_text=traceback_text,
            service_root=service_root,
        )

        if args.json or args.compact_json:
            _print_json(script_result, pretty=not args.compact_json)
        else:
            _print_human_result(script_result)
            if traceback_text:
                print(traceback_text)

        return EXIT_BOOTSTRAP_FAILED if args.fail_on_error else EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())