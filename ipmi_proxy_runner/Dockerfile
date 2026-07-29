FROM python:3.12-slim

WORKDIR /app

RUN pip install --no-cache-dir "uvicorn[standard]" starlette httpx websockets

COPY main.py /app/main.py
COPY entrypoint.sh /app/entrypoint.sh

# Stateless proxy with no persistent volumes, so a plain non-root user (no
# gosu/chown dance needed like the main app's /shared volume) is sufficient.
# If IPMI_SSL_CERTFILE/KEYFILE are ever bind-mounted, ensure they're
# world-readable (or owned by this uid) since the process no longer runs as root.
RUN chmod +x /app/entrypoint.sh \
    && groupadd -g 10001 appuser && useradd -u 10001 -g appuser -M -s /usr/sbin/nologin appuser \
    && chown -R appuser:appuser /app
USER appuser

ENV PYTHONUNBUFFERED=1
# Plain HTTP by default; a front TLS terminator (Cloudflare/nginx/Traefik) adds https.
ENV PORT=8080

EXPOSE 8080

ENTRYPOINT ["/app/entrypoint.sh"]
