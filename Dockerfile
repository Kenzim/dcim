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
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Large/static assets first so they stay cached when app code changes
# Plugins only; tftp, os_templates, disk_images, isos – mount at runtime
COPY tftp/ ./tftp/
COPY app/plugins/ ./app/plugins/

# Application code and migrations (change frequently)
COPY app/ ./app/
COPY scripts/ ./scripts/
COPY alembic/ ./alembic/
COPY alembic.ini ./

# Built frontend from stage above (npm run build → /build/dist)
COPY --from=frontend /build/dist /app/static

ENV STATIC_FILES_PATH=/app/static
ENV PYTHONUNBUFFERED=1
EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]

# -----------------------------------------------------------------------------
# Stage: app-dev – app + dhcpd/tftpd for dev (bind-mount code, uvicorn --reload)
# -----------------------------------------------------------------------------
FROM app AS app-dev
RUN apt-get update && apt-get install -y --no-install-recommends \
    isc-dhcp-server \
    tftpd-hpa \
    && rm -rf /var/lib/apt/lists/*

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

ENV PYTHONUNBUFFERED=1
ENV POLL_INTERVAL=60

CMD ["python", "-m", "scripts.snmp_bandwidth_poller", "--interval", "60"]
