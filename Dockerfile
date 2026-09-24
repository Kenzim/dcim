# -----------------------------------------------------------------------------
# Stage: frontend – build Svelte app for production
# -----------------------------------------------------------------------------
FROM node:20-alpine AS frontend
WORKDIR /build

COPY frontend/package.json frontend/package-lock.json* ./
RUN npm ci || npm install

COPY frontend/ ./
RUN npm run build

# -----------------------------------------------------------------------------
# Stage: app – FastAPI + static frontend
# -----------------------------------------------------------------------------
FROM python:3.12-slim AS app
WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libmariadb-dev-compat \
    libmariadb-dev \
    pkg-config \
    ipmitool \
    gosu \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Large/static assets first so they stay cached when app code changes
# Plugins only; tftp, os_templates, disk_images, isos – mount at runtime
COPY tftp/ ./tftp/
COPY runner_common/ ./runner_common/
COPY app/plugins/ ./app/plugins/

# Application code and migrations (change frequently)
COPY app/ ./app/
COPY scripts/ ./scripts/
COPY alembic/ ./alembic/
COPY alembic.ini ./
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

# Built frontend from stage above (npm run build → /build/dist)
COPY --from=frontend /build/dist /app/static

# Fixed, non-system UID/GID so the shared /shared volume's ownership is
# predictable and can be fixed up at container start (see docker-entrypoint.sh)
# regardless of whether the named volume pre-exists from an earlier root-only
# image or is created fresh.
RUN groupadd -g 10001 appuser && useradd -u 10001 -g appuser -M -s /usr/sbin/nologin appuser \
    && chown -R appuser:appuser /app

ENV STATIC_FILES_PATH=/app/static
ENV PYTHONUNBUFFERED=1
EXPOSE 8000

# Starts as root only long enough to fix ownership of the runtime-mounted
# /shared volume (which may already exist from before this image ran as
# non-root), then execs the real command as the unprivileged appuser.
ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]

# -----------------------------------------------------------------------------
# Stage: app-dev – app + dhcpd/tftpd for dev (bind-mount code, uvicorn --reload)
# -----------------------------------------------------------------------------
FROM app AS app-dev
USER root
RUN apt-get update && apt-get install -y --no-install-recommends \
    isc-dhcp-server \
    tftpd-hpa \
    && rm -rf /var/lib/apt/lists/*
# dhcpd/tftpd bind privileged ports (67, 69) and bind-mounted dev volumes are
# host-root-owned, so this dev-only combined image keeps running as root
# unlike the production `app` stage -- undo the inherited gosu-drop entrypoint.
ENTRYPOINT []
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]

# -----------------------------------------------------------------------------
# Stage: bandwidth-poller – SNMP bandwidth poller (same DB/plugins, no frontend)
# -----------------------------------------------------------------------------
FROM python:3.12-slim AS bandwidth-poller
WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libmariadb-dev-compat \
    libmariadb-dev \
    pkg-config \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ ./app/
COPY scripts/ ./scripts/
COPY app/plugins/ ./app/plugins/

RUN groupadd -g 10001 appuser && useradd -u 10001 -g appuser -M -s /usr/sbin/nologin appuser \
    && chown -R appuser:appuser /app
USER appuser

ENV PYTHONUNBUFFERED=1
ENV POLL_INTERVAL=60

CMD ["python", "-m", "scripts.snmp_bandwidth_poller", "--interval", "60"]
