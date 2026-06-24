# services/vectoplan-chunk/entrypoint.sh
#!/bin/sh

set -eu
( set -o pipefail ) >/dev/null 2>&1 && set -o pipefail || true


# ------------------------------------------------------------------------------
# VECTOPLAN Chunk Service Entrypoint
# ------------------------------------------------------------------------------
#
# Unterstützte Modi:
#
#   runtime / gunicorn
#     Startet den Flask/Gunicorn-Service.
#     Runtime ist read-only und führt kein db.create_all() und kein Default-Seeding aus.
#
#   python / wsgi
#     Startet wsgi.py direkt. Nur für lokale Entwicklung.
#     Auch dieser Modus ist standardmäßig read-only.
#
#   db-bootstrap / bootstrap / init
#     Führt scripts/bootstrap_db.py kontrolliert aus.
#     Dieser Modus darf Tabellen erzeugen, Schema-Drift lokal reparieren und
#     Default-Daten inklusive world_spawn seeden/reparieren.
#
#   check-only / db-check
#     Prüft DB-/Schema-/Seed-Zustand über scripts/bootstrap_db.py --check-only.
#     Dieser Modus ist strikt read-only.
#
# Zentrale Regel:
#   Der normale Runtime-Start verändert die Datenbank nicht.
#   DB-Initialisierung läuft nur explizit über db-bootstrap.
#
# World-Regel:
#   world_spawn = konkrete editierbare WorldInstance.
#   flat        = Template/Provider-Welt, nicht die konkrete Runtime-World.
#
# Hinweis:
#   Die erste Zeile ist projektweit als Pfad-Kommentar gesetzt.
#   Da Compose dieses Script über /bin/sh ausführt, ist der Shebang auf Zeile 2
#   hier technisch unkritisch.
# ------------------------------------------------------------------------------


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

DEFAULT_PROJECT_ID="dev-project"
DEFAULT_UNIVERSE_ID="dev-universe"
DEFAULT_WORLD_ID="world_spawn"
DEFAULT_TEMPLATE_ID="flat"
DEFAULT_PROVIDER_WORLD_ID="flat"
DEFAULT_BLOCK_REGISTRY_ID="debug-blocks"
DEFAULT_BLOCK_REGISTRY_VERSION="1"

BOOTSTRAP_SCRIPT="./scripts/bootstrap_db.py"


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

on_signal() {
  signal_name="$1"
  log_warn "Signal empfangen: ${signal_name}. Beende Prozess."
  exit 143
}

trap 'on_signal INT' INT
trap 'on_signal TERM' TERM
trap 'on_signal HUP' HUP


command_exists() {
  command -v "$1" >/dev/null 2>&1
}

safe_pwd() {
  pwd 2>/dev/null || printf '%s' "."
}

is_true() {
  case "${1:-}" in
    1|true|TRUE|True|yes|YES|Yes|y|Y|on|ON|On|enabled|ENABLED|Enabled|enable|ENABLE|Enable)
      return 0
      ;;
    *)
      return 1
      ;;
  esac
}

is_false() {
  case "${1:-}" in
    0|false|FALSE|False|no|NO|No|n|N|off|OFF|Off|disabled|DISABLED|Disabled|disable|DISABLE|Disable)
      return 0
      ;;
    *)
      return 1
      ;;
  esac
}

bool_text() {
  if is_true "${1:-false}"; then
    printf '%s' "true"
  else
    printf '%s' "false"
  fi
}

first_non_empty() {
  for value in "$@"; do
    if [ -n "${value:-}" ]; then
      printf '%s' "$value"
      return 0
    fi
  done

  printf '%s' ""
  return 0
}

normalize_lower() {
  printf '%s' "${1:-}" | tr '[:upper:]' '[:lower:]'
}

normalize_log_level() {
  case "$(normalize_lower "${1:-}")" in
    debug) printf '%s' "debug" ;;
    info) printf '%s' "info" ;;
    warning|warn) printf '%s' "warning" ;;
    error) printf '%s' "error" ;;
    critical|fatal) printf '%s' "critical" ;;
    *) printf '%s' "$DEFAULT_GUNICORN_LOG_LEVEL" ;;
  esac
}

ensure_uint() {
  value="$1"
  var_name="$2"
  fallback="$3"

  case "$value" in
    ''|*[!0-9]*)
      log_warn "Ungültiger numerischer Wert für ${var_name}: '${value}'. Fallback: ${fallback}."
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
    log_warn "Port außerhalb des gültigen Bereichs für ${var_name}: '${raw_port}'. Fallback: ${DEFAULT_PORT}."
    printf '%s' "$DEFAULT_PORT"
    return 0
  fi

  printf '%s' "$validated_port"
}

require_file() {
  file_path="$1"
  file_label="$2"

  if [ ! -f "$file_path" ]; then
    die "Erforderliche Datei fehlt: ${file_label} (${file_path})"
  fi
}

warn_if_missing_file() {
  file_path="$1"
  file_label="$2"

  if [ ! -f "$file_path" ]; then
    log_warn "Optionale Datei fehlt: ${file_label} (${file_path})"
  fi
}

require_dir() {
  dir_path="$1"
  dir_label="$2"

  if [ ! -d "$dir_path" ]; then
    die "Erforderlicher Ordner fehlt: ${dir_label} (${dir_path})"
  fi
}

warn_if_missing_dir() {
  dir_path="$1"
  dir_label="$2"

  if [ ! -d "$dir_path" ]; then
    log_warn "Optionaler Ordner fehlt: ${dir_label} (${dir_path})"
  fi
}

normalize_mode() {
  raw_mode="$(normalize_lower "${1:-}")"

  case "$raw_mode" in
    runtime|gunicorn|'')
      printf '%s' "gunicorn"
      ;;
    python|wsgi|dev|development)
      printf '%s' "python"
      ;;
    db-bootstrap|bootstrap|init|db_init|db-init|database-bootstrap|database_init|database-bootstrap)
      printf '%s' "db-bootstrap"
      ;;
    check-only|db-check|check|database-check|schema-check|readiness-check)
      printf '%s' "check-only"
      ;;
    shell|sh|bash)
      printf '%s' "shell"
      ;;
    *)
      printf '%s' "$raw_mode"
      ;;
  esac
}

startup_mode_requested() {
  raw_startup_mode="$(first_non_empty \
    "${VECTOPLAN_CHUNK_STARTUP_MODE:-}" \
    "${VECTOPLAN_CHUNK_MODE:-}" \
    "${SERVICE_STARTUP_MODE:-}" \
    "${APP_STARTUP_MODE:-}" \
    "${STARTUP_MODE:-}" \
  )"

  normalized_startup_mode="$(normalize_mode "$raw_startup_mode")"

  case "$normalized_startup_mode" in
    db-bootstrap|check-only|shell)
      printf '%s' "$normalized_startup_mode"
      return 0
      ;;
    *)
      return 1
      ;;
  esac
}

resolve_run_mode() {
  if mode_from_startup="$(startup_mode_requested 2>/dev/null)"; then
    printf '%s' "$mode_from_startup"
    return 0
  fi

  if [ "$#" -gt 0 ]; then
    first_arg_mode="$(normalize_mode "${1:-}")"
    case "$first_arg_mode" in
      gunicorn|python|db-bootstrap|check-only|shell)
        printf '%s' "$first_arg_mode"
        return 0
        ;;
      *)
        :
        ;;
    esac
  fi

  raw_run_mode="$(first_non_empty \
    "${VECTOPLAN_CHUNK_RUN_MODE:-}" \
    "${RUN_MODE:-}" \
    "$DEFAULT_RUN_MODE" \
  )"

  normalize_mode "$raw_run_mode"
}

set_env_value() {
  name="$1"
  value="$2"
  override="${3:-false}"

  if is_true "$override" || [ -z "${!name:-}" ]; then
    export "$name=$value"
  fi
}

is_provider_like_world_id() {
  candidate="$(normalize_lower "${1:-}")"
  template_id="$(normalize_lower "${VECTOPLAN_CHUNK_DEFAULT_TEMPLATE_ID:-$DEFAULT_TEMPLATE_ID}")"
  provider_world_id="$(normalize_lower "${VECTOPLAN_CHUNK_DEFAULT_PROVIDER_WORLD_ID:-$DEFAULT_PROVIDER_WORLD_ID}")"

  case "$candidate" in
    "$template_id"|"$provider_world_id"|"$DEFAULT_TEMPLATE_ID"|"$DEFAULT_PROVIDER_WORLD_ID")
      return 0
      ;;
    *)
      return 1
      ;;
  esac
}

ensure_world_identity_env() {
  concrete_world_id="$(first_non_empty \
    "${VECTOPLAN_CHUNK_DEFAULT_INSTANCE_WORLD_ID:-}" \
    "${VECTOPLAN_CHUNK_DEFAULT_WORLD_INSTANCE_ID:-}" \
    "${VECTOPLAN_CHUNK_DEFAULT_SPAWN_WORLD_ID:-}" \
    "${VECTOPLAN_CHUNK_DEFAULT_WORLD_ID:-}" \
    "$DEFAULT_WORLD_ID" \
  )"

  if is_provider_like_world_id "$concrete_world_id"; then
    log_warn "Konkrete Default-Welt war provider/template-artig (${concrete_world_id}). Setze auf ${DEFAULT_WORLD_ID}."
    concrete_world_id="$DEFAULT_WORLD_ID"
  fi

  export VECTOPLAN_CHUNK_DEFAULT_PROJECT_ID="${VECTOPLAN_CHUNK_DEFAULT_PROJECT_ID:-$DEFAULT_PROJECT_ID}"
  export VECTOPLAN_CHUNK_DEFAULT_UNIVERSE_ID="${VECTOPLAN_CHUNK_DEFAULT_UNIVERSE_ID:-$DEFAULT_UNIVERSE_ID}"
  export VECTOPLAN_CHUNK_DEFAULT_WORLD_ID="$concrete_world_id"
  export VECTOPLAN_CHUNK_DEFAULT_INSTANCE_WORLD_ID="$concrete_world_id"
  export VECTOPLAN_CHUNK_DEFAULT_TEMPLATE_ID="${VECTOPLAN_CHUNK_DEFAULT_TEMPLATE_ID:-$DEFAULT_TEMPLATE_ID}"
  export VECTOPLAN_CHUNK_DEFAULT_PROVIDER_WORLD_ID="${VECTOPLAN_CHUNK_DEFAULT_PROVIDER_WORLD_ID:-$DEFAULT_PROVIDER_WORLD_ID}"
  export VECTOPLAN_CHUNK_DEFAULT_BLOCK_REGISTRY_ID="${VECTOPLAN_CHUNK_DEFAULT_BLOCK_REGISTRY_ID:-$DEFAULT_BLOCK_REGISTRY_ID}"
  export VECTOPLAN_CHUNK_DEFAULT_BLOCK_REGISTRY_VERSION="${VECTOPLAN_CHUNK_DEFAULT_BLOCK_REGISTRY_VERSION:-$DEFAULT_BLOCK_REGISTRY_VERSION}"

  export VECTOPLAN_CHUNK_PROJECT_PROVISIONING_DEFAULT_TEMPLATE_ID="${VECTOPLAN_CHUNK_PROJECT_PROVISIONING_DEFAULT_TEMPLATE_ID:-$VECTOPLAN_CHUNK_DEFAULT_TEMPLATE_ID}"
  export VECTOPLAN_CHUNK_PROJECT_PROVISIONING_DEFAULT_WORLD_ID="${VECTOPLAN_CHUNK_PROJECT_PROVISIONING_DEFAULT_WORLD_ID:-$VECTOPLAN_CHUNK_DEFAULT_WORLD_ID}"

  if is_provider_like_world_id "$VECTOPLAN_CHUNK_PROJECT_PROVISIONING_DEFAULT_WORLD_ID"; then
    log_warn "Provisioning Default-World war provider/template-artig (${VECTOPLAN_CHUNK_PROJECT_PROVISIONING_DEFAULT_WORLD_ID}). Setze auf ${VECTOPLAN_CHUNK_DEFAULT_WORLD_ID}."
    export VECTOPLAN_CHUNK_PROJECT_PROVISIONING_DEFAULT_WORLD_ID="$VECTOPLAN_CHUNK_DEFAULT_WORLD_ID"
  fi
}

enforce_runtime_readonly_env() {
  ensure_world_identity_env

  export VECTOPLAN_CHUNK_MODE="runtime"
  export VECTOPLAN_CHUNK_STARTUP_MODE="runtime"
  export VECTOPLAN_CHUNK_RUNTIME_MODE="runtime"

  export VECTOPLAN_CHUNK_DB_BOOTSTRAP_ENABLED="false"
  export VECTOPLAN_CHUNK_ALLOW_RUNTIME_DB_MUTATIONS="false"
  export VECTOPLAN_CHUNK_RUNTIME_IS_READ_ONLY="true"
  export VECTOPLAN_CHUNK_AUTO_CREATE_ALL="false"
  export VECTOPLAN_CHUNK_AUTO_SEED_DEFAULTS="false"
  export VECTOPLAN_CHUNK_SEED_DEBUG_BLOCKS="false"
  export VECTOPLAN_CHUNK_SEED_DEV_PROJECT="false"
  export VECTOPLAN_CHUNK_DB_BOOTSTRAP_CREATE_ALL="false"
  export VECTOPLAN_CHUNK_DB_BOOTSTRAP_SEED_DEFAULTS="false"
  export VECTOPLAN_CHUNK_DB_BOOTSTRAP_SEED_DEBUG_BLOCKS="false"
  export VECTOPLAN_CHUNK_DB_BOOTSTRAP_SEED_DEV_PROJECT="false"
  export VECTOPLAN_CHUNK_DB_BOOTSTRAP_REPAIR_SEED_INVARIANTS="false"
  export VECTOPLAN_CHUNK_BOOTSTRAP_REPAIR_MISSING_COLUMNS="false"
  export VECTOPLAN_CHUNK_RUN_STARTUP_HOOKS="${VECTOPLAN_CHUNK_RUN_STARTUP_HOOKS:-false}"
}

enforce_check_only_env() {
  ensure_world_identity_env

  export VECTOPLAN_CHUNK_MODE="check-only"
  export VECTOPLAN_CHUNK_STARTUP_MODE="check-only"
  export VECTOPLAN_CHUNK_RUNTIME_MODE="check-only"
  export VECTOPLAN_CHUNK_RUN_MODE="check-only"

  export VECTOPLAN_CHUNK_DB_BOOTSTRAP_ENABLED="false"
  export VECTOPLAN_CHUNK_ALLOW_RUNTIME_DB_MUTATIONS="false"
  export VECTOPLAN_CHUNK_RUNTIME_IS_READ_ONLY="true"
  export VECTOPLAN_CHUNK_AUTO_CREATE_ALL="false"
  export VECTOPLAN_CHUNK_AUTO_SEED_DEFAULTS="false"
  export VECTOPLAN_CHUNK_SEED_DEBUG_BLOCKS="false"
  export VECTOPLAN_CHUNK_SEED_DEV_PROJECT="false"
  export VECTOPLAN_CHUNK_DB_BOOTSTRAP_CREATE_ALL="false"
  export VECTOPLAN_CHUNK_DB_BOOTSTRAP_SEED_DEFAULTS="false"
  export VECTOPLAN_CHUNK_DB_BOOTSTRAP_SEED_DEBUG_BLOCKS="false"
  export VECTOPLAN_CHUNK_DB_BOOTSTRAP_SEED_DEV_PROJECT="false"
  export VECTOPLAN_CHUNK_DB_BOOTSTRAP_REPAIR_SEED_INVARIANTS="false"
  export VECTOPLAN_CHUNK_BOOTSTRAP_REPAIR_MISSING_COLUMNS="false"
  export VECTOPLAN_CHUNK_RUN_STARTUP_HOOKS="false"
}

enforce_bootstrap_env() {
  ensure_world_identity_env

  export VECTOPLAN_CHUNK_MODE="db-bootstrap"
  export VECTOPLAN_CHUNK_STARTUP_MODE="db-bootstrap"
  export VECTOPLAN_CHUNK_RUNTIME_MODE="db-bootstrap"
  export VECTOPLAN_CHUNK_RUN_MODE="db-bootstrap"

  export VECTOPLAN_CHUNK_DB_BOOTSTRAP_ENABLED="true"
  export VECTOPLAN_CHUNK_ALLOW_RUNTIME_DB_MUTATIONS="true"
  export VECTOPLAN_CHUNK_RUNTIME_IS_READ_ONLY="false"

  export VECTOPLAN_CHUNK_AUTO_CREATE_ALL="${VECTOPLAN_CHUNK_AUTO_CREATE_ALL:-true}"
  export VECTOPLAN_CHUNK_AUTO_SEED_DEFAULTS="${VECTOPLAN_CHUNK_AUTO_SEED_DEFAULTS:-true}"
  export VECTOPLAN_CHUNK_SEED_DEBUG_BLOCKS="${VECTOPLAN_CHUNK_SEED_DEBUG_BLOCKS:-true}"
  export VECTOPLAN_CHUNK_SEED_DEV_PROJECT="${VECTOPLAN_CHUNK_SEED_DEV_PROJECT:-true}"

  export VECTOPLAN_CHUNK_DB_BOOTSTRAP_CREATE_ALL="${VECTOPLAN_CHUNK_DB_BOOTSTRAP_CREATE_ALL:-$VECTOPLAN_CHUNK_AUTO_CREATE_ALL}"
  export VECTOPLAN_CHUNK_DB_BOOTSTRAP_SEED_DEFAULTS="${VECTOPLAN_CHUNK_DB_BOOTSTRAP_SEED_DEFAULTS:-$VECTOPLAN_CHUNK_AUTO_SEED_DEFAULTS}"
  export VECTOPLAN_CHUNK_DB_BOOTSTRAP_SEED_DEBUG_BLOCKS="${VECTOPLAN_CHUNK_DB_BOOTSTRAP_SEED_DEBUG_BLOCKS:-$VECTOPLAN_CHUNK_SEED_DEBUG_BLOCKS}"
  export VECTOPLAN_CHUNK_DB_BOOTSTRAP_SEED_DEV_PROJECT="${VECTOPLAN_CHUNK_DB_BOOTSTRAP_SEED_DEV_PROJECT:-$VECTOPLAN_CHUNK_SEED_DEV_PROJECT}"

  export VECTOPLAN_CHUNK_BOOTSTRAP_REPAIR_MISSING_COLUMNS="${VECTOPLAN_CHUNK_BOOTSTRAP_REPAIR_MISSING_COLUMNS:-true}"
  export VECTOPLAN_CHUNK_DB_BOOTSTRAP_REPAIR_SEED_INVARIANTS="${VECTOPLAN_CHUNK_DB_BOOTSTRAP_REPAIR_SEED_INVARIANTS:-true}"
  export VECTOPLAN_CHUNK_SEED_ON_EMPTY_ONLY="${VECTOPLAN_CHUNK_SEED_ON_EMPTY_ONLY:-true}"
  export VECTOPLAN_CHUNK_DB_BOOTSTRAP_FAIL_ON_ERROR="${VECTOPLAN_CHUNK_DB_BOOTSTRAP_FAIL_ON_ERROR:-true}"
  export VECTOPLAN_CHUNK_RUN_STARTUP_HOOKS="false"
}

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

get_db_name() {
  first_non_empty \
    "${VECTOPLAN_CHUNK_DB_NAME:-}" \
    "${VECTOPLAN_CHUNK_POSTGRES_DB:-}" \
    "${POSTGRES_DB:-}" \
    "${DB_NAME:-}" \
    "vectoplan_chunk"
}

get_db_user() {
  first_non_empty \
    "${VECTOPLAN_CHUNK_DB_USER:-}" \
    "${VECTOPLAN_CHUNK_POSTGRES_USER:-}" \
    "${POSTGRES_USER:-}" \
    "${DB_USER:-}" \
    "vectoplan_chunk"
}


APP_HOME="${APP_HOME:-${VECTOPLAN_CHUNK_APP_HOME:-$DEFAULT_APP_HOME}}"

VECTOPLAN_CHUNK_HOST="${VECTOPLAN_CHUNK_HOST:-${HOST:-$DEFAULT_HOST}}"
VECTOPLAN_CHUNK_PORT="${VECTOPLAN_CHUNK_PORT:-${PORT:-$DEFAULT_PORT}}"
VECTOPLAN_CHUNK_CONFIG="${VECTOPLAN_CHUNK_CONFIG:-${APP_CONFIG:-$DEFAULT_CONFIG}}"

VECTOPLAN_CHUNK_PRESTART_CHECK="${VECTOPLAN_CHUNK_PRESTART_CHECK:-true}"
VECTOPLAN_CHUNK_PRINT_STARTUP_SUMMARY="${VECTOPLAN_CHUNK_PRINT_STARTUP_SUMMARY:-true}"
VECTOPLAN_CHUNK_STRICT_ASSET_CHECKS="${VECTOPLAN_CHUNK_STRICT_ASSET_CHECKS:-false}"
VECTOPLAN_CHUNK_STRICT_TEMPLATE_CHECKS="${VECTOPLAN_CHUNK_STRICT_TEMPLATE_CHECKS:-false}"

VECTOPLAN_CHUNK_DB_WAIT_FOR_READY="${VECTOPLAN_CHUNK_DB_WAIT_FOR_READY:-true}"
VECTOPLAN_CHUNK_DB_WAIT_TIMEOUT="${VECTOPLAN_CHUNK_DB_WAIT_TIMEOUT:-60}"
VECTOPLAN_CHUNK_DB_WAIT_INTERVAL="${VECTOPLAN_CHUNK_DB_WAIT_INTERVAL:-2}"
VECTOPLAN_CHUNK_DB_REQUIRE_READY="${VECTOPLAN_CHUNK_DB_REQUIRE_READY:-true}"

VECTOPLAN_CHUNK_SCHEMA_READY_CHECK="${VECTOPLAN_CHUNK_SCHEMA_READY_CHECK:-true}"
VECTOPLAN_CHUNK_SCHEMA_READY_REQUIRED="${VECTOPLAN_CHUNK_SCHEMA_READY_REQUIRED:-true}"
VECTOPLAN_CHUNK_SCHEMA_READY_RETRIES="${VECTOPLAN_CHUNK_SCHEMA_READY_RETRIES:-3}"
VECTOPLAN_CHUNK_SCHEMA_READY_RETRY_SECONDS="${VECTOPLAN_CHUNK_SCHEMA_READY_RETRY_SECONDS:-2}"

VECTOPLAN_CHUNK_HEALTHCHECK_PATH="${VECTOPLAN_CHUNK_HEALTHCHECK_PATH:-/projects/_status}"

VECTOPLAN_CHUNK_INIT_MAX_ATTEMPTS="${VECTOPLAN_CHUNK_INIT_MAX_ATTEMPTS:-5}"
VECTOPLAN_CHUNK_INIT_RETRY_SECONDS="${VECTOPLAN_CHUNK_INIT_RETRY_SECONDS:-3}"
VECTOPLAN_CHUNK_INIT_JSON="${VECTOPLAN_CHUNK_INIT_JSON:-true}"
VECTOPLAN_CHUNK_INIT_VERIFY_AFTER_BOOTSTRAP="${VECTOPLAN_CHUNK_INIT_VERIFY_AFTER_BOOTSTRAP:-true}"

VECTOPLAN_CHUNK_AUTO_CREATE_ALL="${VECTOPLAN_CHUNK_AUTO_CREATE_ALL:-false}"
VECTOPLAN_CHUNK_AUTO_SEED_DEFAULTS="${VECTOPLAN_CHUNK_AUTO_SEED_DEFAULTS:-false}"
VECTOPLAN_CHUNK_SEED_DEBUG_BLOCKS="${VECTOPLAN_CHUNK_SEED_DEBUG_BLOCKS:-false}"
VECTOPLAN_CHUNK_SEED_DEV_PROJECT="${VECTOPLAN_CHUNK_SEED_DEV_PROJECT:-false}"
VECTOPLAN_CHUNK_ALLOW_RUNTIME_DB_MUTATIONS="${VECTOPLAN_CHUNK_ALLOW_RUNTIME_DB_MUTATIONS:-false}"
VECTOPLAN_CHUNK_RUNTIME_IS_READ_ONLY="${VECTOPLAN_CHUNK_RUNTIME_IS_READ_ONLY:-true}"
VECTOPLAN_CHUNK_BOOTSTRAP_REPAIR_MISSING_COLUMNS="${VECTOPLAN_CHUNK_BOOTSTRAP_REPAIR_MISSING_COLUMNS:-false}"
VECTOPLAN_CHUNK_DB_BOOTSTRAP_REPAIR_SEED_INVARIANTS="${VECTOPLAN_CHUNK_DB_BOOTSTRAP_REPAIR_SEED_INVARIANTS:-false}"

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
GUNICORN_LOG_LEVEL="$(normalize_log_level "$GUNICORN_LOG_LEVEL")"

VECTOPLAN_CHUNK_DB_WAIT_TIMEOUT="$(ensure_uint "$VECTOPLAN_CHUNK_DB_WAIT_TIMEOUT" "VECTOPLAN_CHUNK_DB_WAIT_TIMEOUT" "60")"
VECTOPLAN_CHUNK_DB_WAIT_INTERVAL="$(ensure_uint "$VECTOPLAN_CHUNK_DB_WAIT_INTERVAL" "VECTOPLAN_CHUNK_DB_WAIT_INTERVAL" "2")"
VECTOPLAN_CHUNK_SCHEMA_READY_RETRIES="$(ensure_uint "$VECTOPLAN_CHUNK_SCHEMA_READY_RETRIES" "VECTOPLAN_CHUNK_SCHEMA_READY_RETRIES" "3")"
VECTOPLAN_CHUNK_SCHEMA_READY_RETRY_SECONDS="$(ensure_uint "$VECTOPLAN_CHUNK_SCHEMA_READY_RETRY_SECONDS" "VECTOPLAN_CHUNK_SCHEMA_READY_RETRY_SECONDS" "2")"
VECTOPLAN_CHUNK_INIT_MAX_ATTEMPTS="$(ensure_uint "$VECTOPLAN_CHUNK_INIT_MAX_ATTEMPTS" "VECTOPLAN_CHUNK_INIT_MAX_ATTEMPTS" "5")"
VECTOPLAN_CHUNK_INIT_RETRY_SECONDS="$(ensure_uint "$VECTOPLAN_CHUNK_INIT_RETRY_SECONDS" "VECTOPLAN_CHUNK_INIT_RETRY_SECONDS" "3")"


export APP_NAME
export SERVICE_NAME="${SERVICE_NAME:-vectoplan-chunk}"
export VECTOPLAN_SERVICE_NAME="${VECTOPLAN_SERVICE_NAME:-vectoplan-chunk}"
export VECTOPLAN_EXTENSION_NAMESPACE="${VECTOPLAN_EXTENSION_NAMESPACE:-vectoplan_chunk}"
export SERVICE_EXTENSION_NAMESPACE="${SERVICE_EXTENSION_NAMESPACE:-vectoplan_chunk}"
export ROUTES_EXTENSION_NAMESPACE="${ROUTES_EXTENSION_NAMESPACE:-vectoplan_chunk}"

export VECTOPLAN_CHUNK_CONFIG
export VECTOPLAN_CHUNK_HOST
export VECTOPLAN_CHUNK_PORT

# Legacy-/Kompatibilitätsvariablen für ältere Codepfade.
export VECTOPLAN_EDITOR_CONFIG="${VECTOPLAN_EDITOR_CONFIG:-$VECTOPLAN_CHUNK_CONFIG}"
export VECTOPLAN_EDITOR_HOST="${VECTOPLAN_EDITOR_HOST:-$VECTOPLAN_CHUNK_HOST}"
export VECTOPLAN_EDITOR_PORT="${VECTOPLAN_EDITOR_PORT:-$VECTOPLAN_CHUNK_PORT}"
export VECTOPLAN_EDITOR_FRONTEND_BUILD_REQUIRED="${VECTOPLAN_EDITOR_FRONTEND_BUILD_REQUIRED:-false}"
export VECTOPLAN_EDITOR_FRONTEND_STRICT_CHECKS="${VECTOPLAN_EDITOR_FRONTEND_STRICT_CHECKS:-false}"


if [ ! -d "$APP_HOME" ]; then
  die "APP_HOME existiert nicht: ${APP_HOME}"
fi

cd "$APP_HOME" || die "Konnte nicht in APP_HOME wechseln: ${APP_HOME}"

command_exists python || die "'python' ist im Container nicht verfügbar."

log_info "Arbeitsverzeichnis: $(safe_pwd)"
log_info "Python: $(python --version 2>/dev/null || printf '%s' 'unknown')"


validate_common_files() {
  require_file "./app.py" "Flask app factory"
  require_file "./wsgi.py" "WSGI entrypoint"
  require_file "./config.py" "Service configuration"
  require_file "./extensions.py" "Flask extensions"
  require_file "./requirements.txt" "Python requirements"
  require_file "./routes/__init__.py" "Blueprint registration"

  require_dir "./routes" "Routes directory"
  require_dir "./src" "Source directory"
  require_dir "./models" "SQLAlchemy models directory"

  require_file "./models/__init__.py" "Model registration package"
  require_file "./models/project.py" "Project model"
  require_file "./models/universe.py" "Universe model"
  require_file "./models/world.py" "WorldInstance model"
  require_file "./models/block.py" "Block models"
  require_file "./models/chunk.py" "ChunkSnapshot model"
  require_file "./models/event.py" "Command/Event models"
  require_file "./models/object.py" "World object models"
}

validate_runtime_files() {
  validate_common_files

  require_dir "./src/world" "World provider directory"
  require_dir "./src/world_state" "World-state directory"
  require_dir "./src/world/flat" "Flat world provider directory"

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
}

validate_bootstrap_files() {
  validate_common_files
  require_file "$BOOTSTRAP_SCRIPT" "Chunk DB bootstrap script"
}


wait_for_database_socket() {
  db_host="$(get_db_host)"
  db_port="$(get_db_port)"
  db_port="$(ensure_port "$db_port" "VECTOPLAN_CHUNK_DB_PORT")"

  if ! is_true "$VECTOPLAN_CHUNK_DB_WAIT_FOR_READY"; then
    log_warn "DB-Wait wurde durch VECTOPLAN_CHUNK_DB_WAIT_FOR_READY=false übersprungen."
    return 0
  fi

  log_info "Warte auf PostgreSQL-Socket ${db_host}:${db_port}."

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
      log_info "PostgreSQL-Socket erreichbar: ${db_host}:${db_port}."
      return 0
    fi

    if [ "$elapsed" -ge "$VECTOPLAN_CHUNK_DB_WAIT_TIMEOUT" ]; then
      break
    fi

    sleep "$VECTOPLAN_CHUNK_DB_WAIT_INTERVAL"
    elapsed=$((elapsed + VECTOPLAN_CHUNK_DB_WAIT_INTERVAL))
  done

  if is_true "$VECTOPLAN_CHUNK_DB_REQUIRE_READY"; then
    die "PostgreSQL-Socket wurde innerhalb von ${VECTOPLAN_CHUNK_DB_WAIT_TIMEOUT}s nicht erreichbar: ${db_host}:${db_port}"
  fi

  log_warn "PostgreSQL-Socket ist nicht erreichbar, aber DB-Ready ist nicht erforderlich: ${db_host}:${db_port}"
  return 0
}


run_prestart_check() {
  log_info "Starte Python-Prestart-Check."

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

    route_set = set(route_rules)

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

    optional_future_routes = {
        "/projects/by-app/<app_project_public_id>",
        "/projects/ensure",
    }

    missing_required = sorted(required_routes - route_set)
    missing_optional = sorted(optional_future_routes - route_set)

    print("[vectoplan-chunk] Prestart check successful.")
    print(f"[vectoplan-chunk] App Name: {app.config.get('APP_NAME', 'vectoplan-chunk')}")
    print(f"[vectoplan-chunk] Config: {config_name}")
    print(f"[vectoplan-chunk] Route count: {len(route_rules)}")
    print(f"[vectoplan-chunk] Routes: {', '.join(route_rules) if route_rules else 'none detected'}")

    if missing_optional:
        print(
            "[vectoplan-chunk] Optional future routes missing: "
            + ", ".join(missing_optional),
            file=sys.stderr,
        )

    if missing_required:
        print(
            "[vectoplan-chunk] Missing required routes: "
            + ", ".join(missing_required),
            file=sys.stderr,
        )
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
    raise SystemExit(1)
PY
}


build_bootstrap_args() {
  args=""

  args="${args} --config ${VECTOPLAN_CHUNK_CONFIG}"

  if is_true "$VECTOPLAN_CHUNK_AUTO_CREATE_ALL" || is_true "${VECTOPLAN_CHUNK_DB_BOOTSTRAP_CREATE_ALL:-false}"; then
    args="${args} --create-all"
  else
    args="${args} --no-create-all"
  fi

  if is_true "$VECTOPLAN_CHUNK_AUTO_SEED_DEFAULTS" || is_true "$VECTOPLAN_CHUNK_SEED_DEBUG_BLOCKS" || is_true "$VECTOPLAN_CHUNK_SEED_DEV_PROJECT" || is_true "${VECTOPLAN_CHUNK_DB_BOOTSTRAP_SEED_DEFAULTS:-false}"; then
    args="${args} --seed"
  else
    args="${args} --no-seed"
  fi

  if is_true "$VECTOPLAN_CHUNK_BOOTSTRAP_REPAIR_MISSING_COLUMNS"; then
    args="${args} --repair-missing-columns"
  else
    args="${args} --no-repair-missing-columns"
  fi

  if is_true "$VECTOPLAN_CHUNK_DB_BOOTSTRAP_REPAIR_SEED_INVARIANTS"; then
    args="${args} --repair-seed-invariants"
  else
    args="${args} --no-repair-seed-invariants"
  fi

  if is_true "$VECTOPLAN_CHUNK_INIT_JSON"; then
    args="${args} --json"
  fi

  printf '%s' "$args"
}

build_check_only_args() {
  args="--check-only --no-create-all --no-seed --no-repair-missing-columns --no-repair-seed-invariants"

  if is_true "$VECTOPLAN_CHUNK_INIT_JSON"; then
    args="${args} --json"
  fi

  printf '%s' "$args"
}

run_database_bootstrap() {
  enforce_bootstrap_env
  validate_bootstrap_files
  wait_for_database_socket

  log_info "Starte expliziten DB-Bootstrap."

  if ! is_true "$VECTOPLAN_CHUNK_ALLOW_RUNTIME_DB_MUTATIONS"; then
    die "db-bootstrap benötigt VECTOPLAN_CHUNK_ALLOW_RUNTIME_DB_MUTATIONS=true."
  fi

  bootstrap_args="$(build_bootstrap_args)"

  attempt=1

  while [ "$attempt" -le "$VECTOPLAN_CHUNK_INIT_MAX_ATTEMPTS" ]; do
    log_info "DB-Bootstrap-Versuch ${attempt}/${VECTOPLAN_CHUNK_INIT_MAX_ATTEMPTS}: python ${BOOTSTRAP_SCRIPT}${bootstrap_args}"

    set +e
    # shellcheck disable=SC2086
    python "$BOOTSTRAP_SCRIPT" $bootstrap_args
    code="$?"
    set -e

    if [ "$code" -eq 0 ]; then
      if is_true "$VECTOPLAN_CHUNK_INIT_VERIFY_AFTER_BOOTSTRAP"; then
        log_info "DB-Bootstrap abgeschlossen. Starte anschließenden read-only Check."

        set +e
        # shellcheck disable=SC2086
        python "$BOOTSTRAP_SCRIPT" $(build_check_only_args)
        check_code="$?"
        set -e

        if [ "$check_code" -eq 0 ]; then
          log_info "DB-Bootstrap und anschließender Check-only erfolgreich abgeschlossen."
          return 0
        fi

        log_warn "DB-Bootstrap war erfolgreich, aber Check-only ist fehlgeschlagen. Exit-Code: ${check_code}."
        code="$check_code"
      else
        log_info "DB-Bootstrap erfolgreich abgeschlossen."
        return 0
      fi
    fi

    log_warn "DB-Bootstrap-Versuch ${attempt} fehlgeschlagen. Exit-Code: ${code}."

    if [ "$attempt" -ge "$VECTOPLAN_CHUNK_INIT_MAX_ATTEMPTS" ]; then
      die "DB-Bootstrap endgültig fehlgeschlagen nach ${attempt} Versuch(en)."
    fi

    log_info "Warte ${VECTOPLAN_CHUNK_INIT_RETRY_SECONDS}s bis zum nächsten Bootstrap-Versuch."
    sleep "$VECTOPLAN_CHUNK_INIT_RETRY_SECONDS"
    attempt=$((attempt + 1))
  done

  die "Unerwartetes Ende des DB-Bootstrap-Loops."
}

run_database_check_only() {
  enforce_check_only_env
  validate_bootstrap_files
  wait_for_database_socket

  log_info "Starte DB-Check-only."

  check_args="$(build_check_only_args)"

  # shellcheck disable=SC2086
  python "$BOOTSTRAP_SCRIPT" $check_args
}

run_schema_ready_check() {
  enforce_runtime_readonly_env

  if ! is_true "$VECTOPLAN_CHUNK_SCHEMA_READY_CHECK"; then
    log_warn "DB/Seed-Ready-Check wurde durch VECTOPLAN_CHUNK_SCHEMA_READY_CHECK=false übersprungen."
    return 0
  fi

  if [ ! -f "$BOOTSTRAP_SCRIPT" ]; then
    if is_true "$VECTOPLAN_CHUNK_SCHEMA_READY_REQUIRED"; then
      die "DB/Seed-Ready-Check erforderlich, aber ${BOOTSTRAP_SCRIPT} fehlt."
    fi

    log_warn "DB/Seed-Ready-Check übersprungen, weil ${BOOTSTRAP_SCRIPT} fehlt."
    return 0
  fi

  attempt=1

  while [ "$attempt" -le "$VECTOPLAN_CHUNK_SCHEMA_READY_RETRIES" ]; do
    log_info "DB/Seed-Ready-Check Versuch ${attempt}/${VECTOPLAN_CHUNK_SCHEMA_READY_RETRIES}."

    set +e
    # shellcheck disable=SC2086
    python "$BOOTSTRAP_SCRIPT" $(build_check_only_args)
    code="$?"
    set -e

    if [ "$code" -eq 0 ]; then
      log_info "DB/Seed-Ready-Check erfolgreich."
      return 0
    fi

    log_warn "DB/Seed-Ready-Check fehlgeschlagen. Exit-Code: ${code}."

    if [ "$attempt" -ge "$VECTOPLAN_CHUNK_SCHEMA_READY_RETRIES" ]; then
      if is_true "$VECTOPLAN_CHUNK_SCHEMA_READY_REQUIRED"; then
        die "DB/Seed-Ready-Check endgültig fehlgeschlagen. Vermutlich fehlen Tabellen, Spalten oder world_spawn."
      fi

      log_warn "DB/Seed-Ready-Check fehlgeschlagen, aber nicht required."
      return 0
    fi

    sleep "$VECTOPLAN_CHUNK_SCHEMA_READY_RETRY_SECONDS"
    attempt=$((attempt + 1))
  done

  if is_true "$VECTOPLAN_CHUNK_SCHEMA_READY_REQUIRED"; then
    die "Unerwartetes Ende des DB/Seed-Ready-Checks."
  fi

  return 0
}


print_startup_summary() {
  log_info "Service: ${APP_NAME}"
  log_info "Display: ${APP_DISPLAY_NAME}"
  log_info "Run mode: ${RESOLVED_RUN_MODE}"
  log_info "Config: ${VECTOPLAN_CHUNK_CONFIG}"
  log_info "Bind: ${VECTOPLAN_CHUNK_HOST}:${VECTOPLAN_CHUNK_PORT}"
  log_info "Healthcheck path: ${VECTOPLAN_CHUNK_HEALTHCHECK_PATH}"
  log_info "Database host: $(get_db_host)"
  log_info "Database port: $(get_db_port)"
  log_info "Database name: $(get_db_name)"
  log_info "Database user: $(get_db_user)"
  log_info "Runtime read-only: ${VECTOPLAN_CHUNK_RUNTIME_IS_READ_ONLY}"
  log_info "Runtime DB mutations allowed: ${VECTOPLAN_CHUNK_ALLOW_RUNTIME_DB_MUTATIONS}"
  log_info "Auto create all: ${VECTOPLAN_CHUNK_AUTO_CREATE_ALL}"
  log_info "Auto seed defaults: ${VECTOPLAN_CHUNK_AUTO_SEED_DEFAULTS}"
  log_info "Seed debug blocks: ${VECTOPLAN_CHUNK_SEED_DEBUG_BLOCKS}"
  log_info "Seed dev project: ${VECTOPLAN_CHUNK_SEED_DEV_PROJECT}"
  log_info "Repair missing columns: ${VECTOPLAN_CHUNK_BOOTSTRAP_REPAIR_MISSING_COLUMNS}"
  log_info "Repair seed invariants: ${VECTOPLAN_CHUNK_DB_BOOTSTRAP_REPAIR_SEED_INVARIANTS}"
  log_info "Schema/Seed ready check: ${VECTOPLAN_CHUNK_SCHEMA_READY_CHECK}"
  log_info "Schema/Seed ready required: ${VECTOPLAN_CHUNK_SCHEMA_READY_REQUIRED}"
  log_info "Gunicorn App: ${GUNICORN_APP}"
  log_info "Gunicorn Workers: ${GUNICORN_WORKERS}"
  log_info "Gunicorn Threads: ${GUNICORN_THREADS}"
  log_info "Gunicorn Timeout: ${GUNICORN_TIMEOUT}"
  log_info "Gunicorn Keepalive: ${GUNICORN_KEEPALIVE}"
  log_info "Gunicorn Log-Level: ${GUNICORN_LOG_LEVEL}"
  log_info "Default project: ${VECTOPLAN_CHUNK_DEFAULT_PROJECT_ID:-$DEFAULT_PROJECT_ID}"
  log_info "Default universe: ${VECTOPLAN_CHUNK_DEFAULT_UNIVERSE_ID:-$DEFAULT_UNIVERSE_ID}"
  log_info "Default world: ${VECTOPLAN_CHUNK_DEFAULT_WORLD_ID:-$DEFAULT_WORLD_ID}"
  log_info "Default instance world: ${VECTOPLAN_CHUNK_DEFAULT_INSTANCE_WORLD_ID:-$DEFAULT_WORLD_ID}"
  log_info "Default template: ${VECTOPLAN_CHUNK_DEFAULT_TEMPLATE_ID:-$DEFAULT_TEMPLATE_ID}"
  log_info "Provider world: ${VECTOPLAN_CHUNK_DEFAULT_PROVIDER_WORLD_ID:-$DEFAULT_PROVIDER_WORLD_ID}"
}


start_gunicorn() {
  command_exists gunicorn || die "'gunicorn' ist nicht installiert oder nicht im PATH verfügbar."

  enforce_runtime_readonly_env
  validate_runtime_files
  wait_for_database_socket

  if is_true "$VECTOPLAN_CHUNK_PRESTART_CHECK"; then
    run_prestart_check || die "Python-Prestart-Check fehlgeschlagen."
  else
    log_warn "Python-Prestart-Check durch VECTOPLAN_CHUNK_PRESTART_CHECK=false übersprungen."
  fi

  run_schema_ready_check

  if is_true "$VECTOPLAN_CHUNK_PRINT_STARTUP_SUMMARY"; then
    print_startup_summary
  fi

  log_info "Starte ${APP_DISPLAY_NAME} über Gunicorn."

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
  enforce_runtime_readonly_env
  validate_runtime_files
  wait_for_database_socket

  if is_true "$VECTOPLAN_CHUNK_PRESTART_CHECK"; then
    run_prestart_check || die "Python-Prestart-Check fehlgeschlagen."
  else
    log_warn "Python-Prestart-Check durch VECTOPLAN_CHUNK_PRESTART_CHECK=false übersprungen."
  fi

  run_schema_ready_check

  if is_true "$VECTOPLAN_CHUNK_PRINT_STARTUP_SUMMARY"; then
    print_startup_summary
  fi

  log_warn "Starte ${APP_DISPLAY_NAME} im direkten Python-Modus. Nur für Entwicklung empfohlen."
  exec python ./wsgi.py
}

start_shell() {
  shell_bin="/bin/sh"

  if command_exists bash; then
    shell_bin="/bin/bash"
  fi

  log_info "Starte interaktive Shell: ${shell_bin}"
  exec "$shell_bin"
}


RESOLVED_RUN_MODE="$(resolve_run_mode "$@")"

# Wenn erster CLI-Parameter ein erkannter Modus ist, wird er konsumiert.
if [ "$#" -gt 0 ]; then
  first_arg_mode="$(normalize_mode "${1:-}")"
  case "$first_arg_mode" in
    gunicorn|python|db-bootstrap|check-only|shell)
      shift
      ;;
    *)
      :
      ;;
  esac
fi

log_info "Resolved run mode: ${RESOLVED_RUN_MODE}"

# Explizite Custom Commands haben Vorrang, außer es wurde kein erkannter Modus
# als erster Parameter genutzt.
if [ "$#" -gt 0 ]; then
  log_info "Custom command erkannt. Führe aus: $*"
  exec "$@"
fi

case "$RESOLVED_RUN_MODE" in
  gunicorn)
    start_gunicorn
    ;;
  python)
    start_python_wsgi
    ;;
  db-bootstrap)
    enforce_bootstrap_env
    if is_true "$VECTOPLAN_CHUNK_PRINT_STARTUP_SUMMARY"; then
      print_startup_summary
    fi
    run_database_bootstrap
    ;;
  check-only)
    enforce_check_only_env
    if is_true "$VECTOPLAN_CHUNK_PRINT_STARTUP_SUMMARY"; then
      print_startup_summary
    fi
    run_database_check_only
    ;;
  shell)
    ensure_world_identity_env
    start_shell
    ;;
  *)
    die "Unbekannter Run Mode: ${RESOLVED_RUN_MODE}. Erlaubt: gunicorn, python, db-bootstrap, check-only, shell."
    ;;
esac