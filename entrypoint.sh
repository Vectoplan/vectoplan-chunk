# services/vectoplan-chunk/entrypoint.sh
#!/bin/sh

set -eu
( set -o pipefail ) >/dev/null 2>&1 && set -o pipefail || true


APP_NAME="${APP_NAME:-vectoplan-chunk}"
APP_DISPLAY_NAME="${APP_DISPLAY_NAME:-VECTOPLAN Chunk Service}"
DEFAULT_APP_HOME="/opt/vectoplan/services/vectoplan-chunk"
DEFAULT_HOST="0.0.0.0"
DEFAULT_PORT="5000"
DEFAULT_CONFIG="production"
DEFAULT_RUN_MODE="gunicorn"
DEFAULT_GUNICORN_APP="wsgi:app"
DEFAULT_GUNICORN_WORKERS="2"
DEFAULT_GUNICORN_THREADS="2"
DEFAULT_GUNICORN_TIMEOUT="120"
DEFAULT_GUNICORN_KEEPALIVE="5"
DEFAULT_GUNICORN_LOG_LEVEL="info"
DEFAULT_GUNICORN_ACCESSLOG="-"
DEFAULT_GUNICORN_ERRORLOG="-"


timestamp_utc() {
  if command -v date >/dev/null 2>&1; then
    date -u +"%Y-%m-%dT%H:%M:%SZ" 2>/dev/null || printf '%s' "1970-01-01T00:00:00Z"
  else
    printf '%s' "1970-01-01T00:00:00Z"
  fi
}

log_info() {
  printf '%s [INFO]  [%s] %s\n' "$(timestamp_utc)" "$APP_NAME" "$*"
}

log_warn() {
  printf '%s [WARN]  [%s] %s\n' "$(timestamp_utc)" "$APP_NAME" "$*" >&2
}

log_error() {
  printf '%s [ERROR] [%s] %s\n' "$(timestamp_utc)" "$APP_NAME" "$*" >&2
}

die() {
  log_error "$*"
  exit 1
}

command_exists() {
  command -v "$1" >/dev/null 2>&1
}

is_true() {
  case "${1:-}" in
    1|true|TRUE|True|yes|YES|Yes|y|Y|on|ON|On|enabled|ENABLED|Enabled)
      return 0
      ;;
    *)
      return 1
      ;;
  esac
}

is_false() {
  case "${1:-}" in
    0|false|FALSE|False|no|NO|No|n|N|off|OFF|Off|disabled|DISABLED|Disabled)
      return 0
      ;;
    *)
      return 1
      ;;
  esac
}

normalize_log_level() {
  case "${1:-}" in
    debug|DEBUG|Debug) printf '%s' "debug" ;;
    info|INFO|Info) printf '%s' "info" ;;
    warning|WARNING|Warning|warn|WARN|Warn) printf '%s' "warning" ;;
    error|ERROR|Error) printf '%s' "error" ;;
    critical|CRITICAL|Critical) printf '%s' "critical" ;;
    *) printf '%s' "$DEFAULT_GUNICORN_LOG_LEVEL" ;;
  esac
}

require_file() {
  file_path="$1"
  file_label="$2"

  if [ ! -f "$file_path" ]; then
    die "Required file missing: ${file_label} (${file_path})"
  fi
}

warn_if_missing_file() {
  file_path="$1"
  file_label="$2"

  if [ ! -f "$file_path" ]; then
    log_warn "Optional file missing: ${file_label} (${file_path})"
  fi
}

require_dir() {
  dir_path="$1"
  dir_label="$2"

  if [ ! -d "$dir_path" ]; then
    die "Required directory missing: ${dir_label} (${dir_path})"
  fi
}

warn_if_missing_dir() {
  dir_path="$1"
  dir_label="$2"

  if [ ! -d "$dir_path" ]; then
    log_warn "Optional directory missing: ${dir_label} (${dir_path})"
  fi
}

ensure_uint() {
  value="$1"
  var_name="$2"
  fallback="$3"

  case "$value" in
    ''|*[!0-9]*)
      log_warn "Invalid numeric value for ${var_name}: '${value}'. Falling back to ${fallback}."
      printf '%s' "$fallback"
      ;;
    *)
      printf '%s' "$value"
      ;;
  esac
}

ensure_port() {
  raw_port="$1"
  var_name="$2"
  validated_port="$(ensure_uint "$raw_port" "$var_name" "$DEFAULT_PORT")"

  if [ "$validated_port" -lt 1 ] || [ "$validated_port" -gt 65535 ]; then
    log_warn "Port out of range for ${var_name}: '${raw_port}'. Falling back to ${DEFAULT_PORT}."
    printf '%s' "$DEFAULT_PORT"
    return
  fi

  printf '%s' "$validated_port"
}

safe_pwd() {
  pwd 2>/dev/null || printf '%s' "."
}

first_non_empty() {
  for value in "$@"; do
    if [ -n "${value:-}" ]; then
      printf '%s' "$value"
      return 0
    fi
  done

  return 1
}


APP_HOME="${APP_HOME:-${VECTOPLAN_CHUNK_APP_HOME:-$DEFAULT_APP_HOME}}"

VECTOPLAN_CHUNK_HOST="${VECTOPLAN_CHUNK_HOST:-${HOST:-$DEFAULT_HOST}}"
VECTOPLAN_CHUNK_PORT="${VECTOPLAN_CHUNK_PORT:-${PORT:-$DEFAULT_PORT}}"
VECTOPLAN_CHUNK_CONFIG="${VECTOPLAN_CHUNK_CONFIG:-${APP_CONFIG:-$DEFAULT_CONFIG}}"
VECTOPLAN_CHUNK_RUN_MODE="${VECTOPLAN_CHUNK_RUN_MODE:-$DEFAULT_RUN_MODE}"

VECTOPLAN_CHUNK_PRESTART_CHECK="${VECTOPLAN_CHUNK_PRESTART_CHECK:-true}"
VECTOPLAN_CHUNK_PRINT_STARTUP_SUMMARY="${VECTOPLAN_CHUNK_PRINT_STARTUP_SUMMARY:-true}"
VECTOPLAN_CHUNK_STRICT_ASSET_CHECKS="${VECTOPLAN_CHUNK_STRICT_ASSET_CHECKS:-false}"
VECTOPLAN_CHUNK_STRICT_TEMPLATE_CHECKS="${VECTOPLAN_CHUNK_STRICT_TEMPLATE_CHECKS:-false}"

VECTOPLAN_CHUNK_DB_WAIT_FOR_READY="${VECTOPLAN_CHUNK_DB_WAIT_FOR_READY:-true}"
VECTOPLAN_CHUNK_DB_WAIT_TIMEOUT="${VECTOPLAN_CHUNK_DB_WAIT_TIMEOUT:-60}"
VECTOPLAN_CHUNK_DB_WAIT_INTERVAL="${VECTOPLAN_CHUNK_DB_WAIT_INTERVAL:-2}"
VECTOPLAN_CHUNK_DB_REQUIRE_READY="${VECTOPLAN_CHUNK_DB_REQUIRE_READY:-true}"

VECTOPLAN_CHUNK_HEALTHCHECK_PATH="${VECTOPLAN_CHUNK_HEALTHCHECK_PATH:-/projects/_status}"

GUNICORN_APP="${GUNICORN_APP:-$DEFAULT_GUNICORN_APP}"
GUNICORN_WORKERS="${GUNICORN_WORKERS:-$DEFAULT_GUNICORN_WORKERS}"
GUNICORN_THREADS="${GUNICORN_THREADS:-$DEFAULT_GUNICORN_THREADS}"
GUNICORN_TIMEOUT="${GUNICORN_TIMEOUT:-$DEFAULT_GUNICORN_TIMEOUT}"
GUNICORN_KEEPALIVE="${GUNICORN_KEEPALIVE:-$DEFAULT_GUNICORN_KEEPALIVE}"
GUNICORN_LOG_LEVEL="${GUNICORN_LOG_LEVEL:-$DEFAULT_GUNICORN_LOG_LEVEL}"
GUNICORN_ACCESSLOG="${GUNICORN_ACCESSLOG:-$DEFAULT_GUNICORN_ACCESSLOG}"
GUNICORN_ERRORLOG="${GUNICORN_ERRORLOG:-$DEFAULT_GUNICORN_ERRORLOG}"


VECTOPLAN_CHUNK_PORT="$(ensure_port "$VECTOPLAN_CHUNK_PORT" "VECTOPLAN_CHUNK_PORT")"
GUNICORN_WORKERS="$(ensure_uint "$GUNICORN_WORKERS" "GUNICORN_WORKERS" "$DEFAULT_GUNICORN_WORKERS")"
GUNICORN_THREADS="$(ensure_uint "$GUNICORN_THREADS" "GUNICORN_THREADS" "$DEFAULT_GUNICORN_THREADS")"
GUNICORN_TIMEOUT="$(ensure_uint "$GUNICORN_TIMEOUT" "GUNICORN_TIMEOUT" "$DEFAULT_GUNICORN_TIMEOUT")"
GUNICORN_KEEPALIVE="$(ensure_uint "$GUNICORN_KEEPALIVE" "GUNICORN_KEEPALIVE" "$DEFAULT_GUNICORN_KEEPALIVE")"
VECTOPLAN_CHUNK_DB_WAIT_TIMEOUT="$(ensure_uint "$VECTOPLAN_CHUNK_DB_WAIT_TIMEOUT" "VECTOPLAN_CHUNK_DB_WAIT_TIMEOUT" "60")"
VECTOPLAN_CHUNK_DB_WAIT_INTERVAL="$(ensure_uint "$VECTOPLAN_CHUNK_DB_WAIT_INTERVAL" "VECTOPLAN_CHUNK_DB_WAIT_INTERVAL" "2")"
GUNICORN_LOG_LEVEL="$(normalize_log_level "$GUNICORN_LOG_LEVEL")"


export APP_NAME="$APP_NAME"
export SERVICE_NAME="${SERVICE_NAME:-vectoplan-chunk}"
export VECTOPLAN_SERVICE_NAME="${VECTOPLAN_SERVICE_NAME:-vectoplan-chunk}"
export VECTOPLAN_EXTENSION_NAMESPACE="${VECTOPLAN_EXTENSION_NAMESPACE:-vectoplan_chunk}"
export SERVICE_EXTENSION_NAMESPACE="${SERVICE_EXTENSION_NAMESPACE:-vectoplan_chunk}"
export ROUTES_EXTENSION_NAMESPACE="${ROUTES_EXTENSION_NAMESPACE:-vectoplan_chunk}"

export VECTOPLAN_CHUNK_CONFIG
export VECTOPLAN_CHUNK_HOST
export VECTOPLAN_CHUNK_PORT

export VECTOPLAN_EDITOR_CONFIG="${VECTOPLAN_EDITOR_CONFIG:-$VECTOPLAN_CHUNK_CONFIG}"
export VECTOPLAN_EDITOR_HOST="${VECTOPLAN_EDITOR_HOST:-$VECTOPLAN_CHUNK_HOST}"
export VECTOPLAN_EDITOR_PORT="${VECTOPLAN_EDITOR_PORT:-$VECTOPLAN_CHUNK_PORT}"
export VECTOPLAN_EDITOR_RUN_MODE="${VECTOPLAN_EDITOR_RUN_MODE:-$VECTOPLAN_CHUNK_RUN_MODE}"
export VECTOPLAN_EDITOR_PRESTART_CHECK="${VECTOPLAN_EDITOR_PRESTART_CHECK:-$VECTOPLAN_CHUNK_PRESTART_CHECK}"
export VECTOPLAN_EDITOR_PRINT_STARTUP_SUMMARY="${VECTOPLAN_EDITOR_PRINT_STARTUP_SUMMARY:-$VECTOPLAN_CHUNK_PRINT_STARTUP_SUMMARY}"
export VECTOPLAN_EDITOR_FRONTEND_BUILD_REQUIRED="${VECTOPLAN_EDITOR_FRONTEND_BUILD_REQUIRED:-false}"
export VECTOPLAN_EDITOR_FRONTEND_STRICT_CHECKS="${VECTOPLAN_EDITOR_FRONTEND_STRICT_CHECKS:-false}"


if [ ! -d "$APP_HOME" ]; then
  die "APP_HOME does not exist: ${APP_HOME}"
fi

cd "$APP_HOME" || die "Could not change into APP_HOME: ${APP_HOME}"


command_exists python || die "'python' is not available in the container."
command_exists gunicorn || log_warn "'gunicorn' is not available in PATH. Gunicorn run mode will fail."

log_info "Working directory: $(safe_pwd)"
log_info "Python: $(python --version 2>/dev/null || printf '%s' 'unknown')"


require_file "./app.py" "Flask app factory"
require_file "./wsgi.py" "WSGI entrypoint"
require_file "./config.py" "Service configuration"
require_file "./extensions.py" "Flask extensions"
require_file "./requirements.txt" "Python requirements"
require_file "./routes/__init__.py" "Blueprint registration"

require_dir "./routes" "Routes directory"
require_dir "./src" "Source directory"
require_dir "./models" "SQLAlchemy models directory"
require_dir "./src/world" "World provider directory"
require_dir "./src/world_state" "World-state directory"
require_dir "./src/world/flat" "Flat world provider directory"

require_file "./models/__init__.py" "Model registration package"
require_file "./models/project.py" "Project model"
require_file "./models/universe.py" "Universe model"
require_file "./models/world.py" "WorldInstance model"
require_file "./models/block.py" "Block models"
require_file "./models/chunk.py" "ChunkSnapshot model"
require_file "./models/event.py" "Command/Event models"
require_file "./models/object.py" "World object models"

require_file "./routes/projects.py" "Project routes"
require_file "./routes/worlds.py" "World routes"
require_file "./routes/blocks.py" "Block routes"
require_file "./routes/chunks.py" "Chunk routes"
require_file "./routes/commands.py" "Command routes"

require_file "./src/world/flat/world.json" "Flat provider world config"

warn_if_missing_file "./routes/world_test.py" "World-test debug route"
warn_if_missing_file "./routes/editor.py" "Legacy editor route"

warn_if_missing_dir "./migrations" "Alembic migrations directory"
warn_if_missing_dir "./templates" "Legacy template directory"
warn_if_missing_dir "./static" "Legacy static directory"

if is_true "$VECTOPLAN_CHUNK_STRICT_TEMPLATE_CHECKS"; then
  require_dir "./templates" "Template directory"
fi

if is_true "$VECTOPLAN_CHUNK_STRICT_ASSET_CHECKS"; then
  require_dir "./static" "Static directory"
fi


get_db_host() {
  first_non_empty \
    "${VECTOPLAN_CHUNK_DB_HOST:-}" \
    "${VECTOPLAN_CHUNK_POSTGRES_HOST:-}" \
    "${POSTGRES_HOST:-}" \
    "${DB_HOST:-}" \
    "vectoplan-chunk-db"
}

get_db_port() {
  first_non_empty \
    "${VECTOPLAN_CHUNK_DB_PORT:-}" \
    "${VECTOPLAN_CHUNK_POSTGRES_PORT:-}" \
    "${POSTGRES_PORT:-}" \
    "${DB_PORT:-}" \
    "5432"
}

wait_for_database_socket() {
  db_host="$(get_db_host)"
  db_port="$(get_db_port)"

  db_port="$(ensure_port "$db_port" "VECTOPLAN_CHUNK_DB_PORT")"

  if ! is_true "$VECTOPLAN_CHUNK_DB_WAIT_FOR_READY"; then
    log_warn "Database wait was skipped by VECTOPLAN_CHUNK_DB_WAIT_FOR_READY=false."
    return 0
  fi

  log_info "Waiting for PostgreSQL socket ${db_host}:${db_port}."

  elapsed=0

  while [ "$elapsed" -le "$VECTOPLAN_CHUNK_DB_WAIT_TIMEOUT" ]; do
    if python - "$db_host" "$db_port" <<'PY'
import socket
import sys

host = sys.argv[1]
port = int(sys.argv[2])

try:
    with socket.create_connection((host, port), timeout=2):
        pass
except OSError:
    raise SystemExit(1)

raise SystemExit(0)
PY
    then
      log_info "PostgreSQL socket is reachable: ${db_host}:${db_port}."
      return 0
    fi

    if [ "$elapsed" -ge "$VECTOPLAN_CHUNK_DB_WAIT_TIMEOUT" ]; then
      break
    fi

    sleep "$VECTOPLAN_CHUNK_DB_WAIT_INTERVAL"
    elapsed=$((elapsed + VECTOPLAN_CHUNK_DB_WAIT_INTERVAL))
  done

  if is_true "$VECTOPLAN_CHUNK_DB_REQUIRE_READY"; then
    die "PostgreSQL socket did not become reachable within ${VECTOPLAN_CHUNK_DB_WAIT_TIMEOUT}s: ${db_host}:${db_port}"
  fi

  log_warn "PostgreSQL socket is not reachable, but DB readiness is not required: ${db_host}:${db_port}"
  return 0
}


run_prestart_check() {
  log_info "Starting Python bootstrap check."

  python <<'PY'
import os
import sys

try:
    from app import create_app

    config_name = os.getenv("VECTOPLAN_CHUNK_CONFIG") or os.getenv("APP_CONFIG") or "production"
    app = create_app(config_name)

    route_rules = []
    try:
        route_rules = sorted(str(rule.rule) for rule in app.url_map.iter_rules())
    except Exception:
        route_rules = []

    required_routes = {
        "/projects/_status",
        "/worlds/_status",
        "/blocks/_status",
        "/chunks/_status",
        "/commands/_status",
        "/projects/<project_id>/bootstrap",
        "/projects/<project_id>/worlds",
        "/projects/<project_id>/worlds/<world_id>",
        "/projects/<project_id>/worlds/<world_id>/blocks",
        "/projects/<project_id>/worlds/<world_id>/chunks",
        "/projects/<project_id>/worlds/<world_id>/chunks/batch",
        "/projects/<project_id>/worlds/<world_id>/commands",
    }

    route_set = set(route_rules)
    missing = sorted(required_routes - route_set)

    print("[vectoplan-chunk] Prestart check successful.")
    print(f"[vectoplan-chunk] App Name: {app.config.get('APP_NAME', 'vectoplan-chunk')}")
    print(f"[vectoplan-chunk] Config: {config_name}")
    print(f"[vectoplan-chunk] Route count: {len(route_rules)}")
    print(f"[vectoplan-chunk] Routes: {', '.join(route_rules) if route_rules else 'none detected'}")

    if missing:
        print(f"[vectoplan-chunk] Missing required routes: {', '.join(missing)}", file=sys.stderr)
        raise SystemExit(2)

    try:
        from extensions import get_database_status
        status = get_database_status(app, check_connection=True)
        print(f"[vectoplan-chunk] Database configured: {status.get('configured')}")
        print(f"[vectoplan-chunk] Database connection checked: {status.get('connectionChecked')}")
        print(f"[vectoplan-chunk] Database connection ok: {status.get('connectionOk')}")
        if status.get("connectionChecked") and not status.get("connectionOk"):
            print(f"[vectoplan-chunk] Database error: {status.get('connectionError')}", file=sys.stderr)
            raise SystemExit(3)
    except SystemExit:
        raise
    except Exception as db_exc:
        print(f"[vectoplan-chunk] Database status check failed: {db_exc!r}", file=sys.stderr)
        raise SystemExit(4)

except SystemExit:
    raise
except Exception as exc:
    print(f"[vectoplan-chunk] Prestart check failed: {exc!r}", file=sys.stderr)
    raise
PY
}


wait_for_database_socket

if is_true "$VECTOPLAN_CHUNK_PRESTART_CHECK"; then
  run_prestart_check || die "Python bootstrap check failed."
else
  log_warn "Python bootstrap check skipped by VECTOPLAN_CHUNK_PRESTART_CHECK=false."
fi


print_startup_summary() {
  log_info "Service: ${APP_NAME}"
  log_info "Display: ${APP_DISPLAY_NAME}"
  log_info "Run mode: ${VECTOPLAN_CHUNK_RUN_MODE}"
  log_info "Config: ${VECTOPLAN_CHUNK_CONFIG}"
  log_info "Bind: ${VECTOPLAN_CHUNK_HOST}:${VECTOPLAN_CHUNK_PORT}"
  log_info "Healthcheck path: ${VECTOPLAN_CHUNK_HEALTHCHECK_PATH}"
  log_info "Database host: $(get_db_host)"
  log_info "Database port: $(get_db_port)"
  log_info "Gunicorn App: ${GUNICORN_APP}"
  log_info "Gunicorn Workers: ${GUNICORN_WORKERS}"
  log_info "Gunicorn Threads: ${GUNICORN_THREADS}"
  log_info "Gunicorn Timeout: ${GUNICORN_TIMEOUT}"
  log_info "Gunicorn Keepalive: ${GUNICORN_KEEPALIVE}"
  log_info "Gunicorn Log-Level: ${GUNICORN_LOG_LEVEL}"
  log_info "Project default: ${VECTOPLAN_CHUNK_DEFAULT_PROJECT_ID:-dev-project}"
  log_info "Universe default: ${VECTOPLAN_CHUNK_DEFAULT_UNIVERSE_ID:-dev-universe}"
  log_info "World default: ${VECTOPLAN_CHUNK_DEFAULT_INSTANCE_WORLD_ID:-world_spawn}"
  log_info "Provider world: ${VECTOPLAN_CHUNK_DEFAULT_PROVIDER_WORLD_ID:-flat}"
}

if is_true "$VECTOPLAN_CHUNK_PRINT_STARTUP_SUMMARY"; then
  print_startup_summary
fi


if [ "$#" -gt 0 ]; then
  log_info "Custom command detected. Executing: $*"
  exec "$@"
fi


start_gunicorn() {
  command_exists gunicorn || die "'gunicorn' is not installed or not available in PATH."

  log_info "Starting ${APP_DISPLAY_NAME} through Gunicorn."

  exec gunicorn \
    --bind "${VECTOPLAN_CHUNK_HOST}:${VECTOPLAN_CHUNK_PORT}" \
    --workers "${GUNICORN_WORKERS}" \
    --threads "${GUNICORN_THREADS}" \
    --timeout "${GUNICORN_TIMEOUT}" \
    --keep-alive "${GUNICORN_KEEPALIVE}" \
    --log-level "${GUNICORN_LOG_LEVEL}" \
    --access-logfile "${GUNICORN_ACCESSLOG}" \
    --error-logfile "${GUNICORN_ERRORLOG}" \
    "${GUNICORN_APP}"
}

start_python_wsgi() {
  log_warn "Starting ${APP_DISPLAY_NAME} in direct Python mode. This is intended mainly for development."
  exec python ./wsgi.py
}

case "$VECTOPLAN_CHUNK_RUN_MODE" in
  gunicorn)
    start_gunicorn
    ;;
  python|wsgi)
    start_python_wsgi
    ;;
  *)
    die "Unknown VECTOPLAN_CHUNK_RUN_MODE: ${VECTOPLAN_CHUNK_RUN_MODE}. Allowed: gunicorn, python, wsgi."
    ;;
esac