FROM python:3.12-slim

WORKDIR /app

RUN pip install --no-cache-dir "uvicorn[standard]" starlette httpx websockets

COPY main.py /app/main.py
COPY entrypoint.sh /app/entrypoint.sh
RUN chmod +x /app/entrypoint.sh

ENV PYTHONUNBUFFERED=1
# Plain HTTP by default; a front TLS terminator (Cloudflare/nginx/Traefik) adds https.
ENV PORT=8080

EXPOSE 8080

ENTRYPOINT ["/app/entrypoint.sh"]
