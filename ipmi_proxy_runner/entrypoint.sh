#!/bin/sh
set -e

# Wildcard TLS for *.ipmi.<base> is terminated here. Mount the cert/key and set
# IPMI_SSL_CERTFILE / IPMI_SSL_KEYFILE. If unset, the runner serves plain HTTP
# (only appropriate behind a separate TLS terminator).
ARGS="main:app --host 0.0.0.0 --port ${PORT:-443}"

if [ -n "$IPMI_SSL_CERTFILE" ] && [ -n "$IPMI_SSL_KEYFILE" ]; then
    ARGS="$ARGS --ssl-certfile $IPMI_SSL_CERTFILE --ssl-keyfile $IPMI_SSL_KEYFILE"
fi

exec uvicorn $ARGS
