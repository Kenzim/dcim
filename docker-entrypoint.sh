#!/bin/sh
# Fix ownership of the runtime-mounted /shared volume (DHCP/TFTP config
# staging) before dropping from root to the unprivileged `appuser`, then exec
# the real command as that user. This runs on every container start (cheap
# for a small handful of config files) so it works whether /shared is a fresh
# named volume or one already populated from before this image ran as
# non-root -- no manual one-time migration step required on deploy.
set -e

if [ -d /shared ]; then
    chown -R appuser:appuser /shared
fi

exec gosu appuser "$@"
