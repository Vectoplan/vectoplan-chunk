# services/vectoplan-chunk/Dockerfile

FROM python:3.12-slim AS runtime


ARG APP_HOME=/opt/vectoplan/services/vectoplan-chunk
ARG APP_USER=vectoplan
ARG APP_UID=10003
ARG APP_GID=10003


LABEL org.opencontainers.image.title="vectoplan-chunk" \
      org.opencontainers.image.description="VECTOPLAN Chunk Service Flask/Python/PostgreSQL service" \
      org.opencontainers.image.vendor="VECTOPLAN"


ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=${APP_HOME} \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    SERVICE_NAME=vectoplan-chunk \
    APP_NAME=vectoplan-chunk \
    APP_HOME=${APP_HOME} \
    VECTOPLAN_SERVICE_NAME=vectoplan-chunk \
    VECTOPLAN_EXTENSION_NAMESPACE=vectoplan_chunk \
    SERVICE_EXTENSION_NAMESPACE=vectoplan_chunk \
    ROUTES_EXTENSION_NAMESPACE=vectoplan_chunk \
    VECTOPLAN_CHUNK_HOST=0.0.0.0 \
    VECTOPLAN_CHUNK_PORT=5000 \
    VECTOPLAN_CHUNK_CONFIG=production \
    VECTOPLAN_CHUNK_RUN_MODE=gunicorn \
    VECTOPLAN_CHUNK_HEALTHCHECK_PATH=/projects/_status \
    VECTOPLAN_CHUNK_DB_WAIT_FOR_READY=true \
    VECTOPLAN_CHUNK_DB_REQUIRE_READY=true \
    VECTOPLAN_CHUNK_DB_CHECK_ON_STARTUP=true \
    VECTOPLAN_CHUNK_DB_REQUIRE_ON_STARTUP=true \
    VECTOPLAN_CHUNK_AUTO_CREATE_ALL=false \
    VECTOPLAN_CHUNK_AUTO_SEED_DEFAULTS=true \
    VECTOPLAN_CHUNK_SEED_DEBUG_BLOCKS=true \
    VECTOPLAN_CHUNK_SEED_DEV_PROJECT=true \
    VECTOPLAN_CHUNK_DEFAULT_PROJECT_ID=dev-project \
    VECTOPLAN_CHUNK_DEFAULT_UNIVERSE_ID=dev-universe \
    VECTOPLAN_CHUNK_DEFAULT_INSTANCE_WORLD_ID=world_spawn \
    VECTOPLAN_CHUNK_DEFAULT_WORLD_TEMPLATE_ID=flat \
    VECTOPLAN_CHUNK_DEFAULT_PROVIDER_WORLD_ID=flat \
    VECTOPLAN_CHUNK_DEFAULT_PROVIDER_ID=flat \
    VECTOPLAN_EDITOR_CONFIG=production \
    VECTOPLAN_EDITOR_HOST=0.0.0.0 \
    VECTOPLAN_EDITOR_PORT=5000 \
    VECTOPLAN_EDITOR_RUN_MODE=gunicorn \
    VECTOPLAN_EDITOR_FRONTEND_BUILD_REQUIRED=false \
    VECTOPLAN_EDITOR_FRONTEND_STRICT_CHECKS=false \
    GUNICORN_APP=wsgi:app \
    GUNICORN_WORKERS=2 \
    GUNICORN_THREADS=2 \
    GUNICORN_TIMEOUT=120 \
    GUNICORN_KEEPALIVE=5 \
    GUNICORN_LOG_LEVEL=info \
    GUNICORN_ACCESSLOG=- \
    GUNICORN_ERRORLOG=-


WORKDIR ${APP_HOME}


RUN set -eux; \
    if ! getent group "${APP_USER}" > /dev/null 2>&1; then \
        addgroup --system --gid "${APP_GID}" "${APP_USER}"; \
    fi; \
    if ! id -u "${APP_USER}" > /dev/null 2>&1; then \
        adduser \
            --system \
            --uid "${APP_UID}" \
            --ingroup "${APP_USER}" \
            --home "${APP_HOME}" \
            --shell /usr/sbin/nologin \
            "${APP_USER}"; \
    fi; \
    mkdir -p "${APP_HOME}"; \
    chown -R "${APP_USER}:${APP_USER}" "${APP_HOME}"


COPY requirements.txt ./


RUN set -eux; \
    python -m pip install --upgrade pip; \
    python -m pip install --requirement requirements.txt; \
    python -m pip check


COPY . .


RUN set -eux; \
    if [ -f "./entrypoint.sh" ]; then \
        sed -i 's/\r$//' ./entrypoint.sh || true; \
        chmod +x ./entrypoint.sh; \
    fi; \
    find "${APP_HOME}" -type d -name "__pycache__" -prune -exec rm -rf {} + || true; \
    find "${APP_HOME}" -type f -name "*.pyc" -delete || true; \
    chown -R "${APP_USER}:${APP_USER}" "${APP_HOME}"


USER ${APP_USER}


EXPOSE 5000


STOPSIGNAL SIGTERM


HEALTHCHECK --interval=30s --timeout=5s --start-period=45s --retries=5 \
  CMD python -c "import os,sys,urllib.request; port=os.getenv('VECTOPLAN_CHUNK_PORT','5000'); path=os.getenv('VECTOPLAN_CHUNK_HEALTHCHECK_PATH','/projects/_status'); url='http://127.0.0.1:%s%s' % (port, path); resp=urllib.request.urlopen(url, timeout=3); sys.exit(0 if 200 <= getattr(resp,'status',200) < 400 else 1)" || exit 1


CMD ["/bin/sh", "-c", "if [ -x ./entrypoint.sh ]; then exec ./entrypoint.sh; else exec gunicorn --bind ${VECTOPLAN_CHUNK_HOST:-0.0.0.0}:${VECTOPLAN_CHUNK_PORT:-5000} --workers ${GUNICORN_WORKERS:-2} --threads ${GUNICORN_THREADS:-2} --timeout ${GUNICORN_TIMEOUT:-120} --keep-alive ${GUNICORN_KEEPALIVE:-5} --log-level ${GUNICORN_LOG_LEVEL:-info} --access-logfile ${GUNICORN_ACCESSLOG:--} --error-logfile ${GUNICORN_ERRORLOG:--} ${GUNICORN_APP:-wsgi:app}; fi"]