FROM golang:1.22-bookworm AS build
WORKDIR /src
COPY go.mod ./
COPY . .
ARG VERSION=0.1.0
RUN CGO_ENABLED=0 GOOS=linux go build -trimpath -ldflags="-s -w -X main.version=${VERSION}" -o /out/storage-runner ./cmd/storage-runner

# The runner changes host networking and drives Ceph and podman, so it runs as
# root with the host network; see README.md for the required flags.
# ceph-common comes from the Ceph Tentacle repository: clusters on Ceph
# 20.2.4+ only accept aes256k cephx keys, which older clients (bookworm ships
# Ceph 16) cannot use. The build fails if the installed client is older.
FROM debian:bookworm-slim
ARG CEPH_MIN_VERSION=20.2.4
ADD https://download.ceph.com/keys/release.asc /etc/apt/keyrings/ceph.asc
RUN set -eu; \
    chmod 0644 /etc/apt/keyrings/ceph.asc; \
    apt-get update; \
    apt-get install -y --no-install-recommends ca-certificates; \
    echo "deb [signed-by=/etc/apt/keyrings/ceph.asc] https://download.ceph.com/debian-tentacle bookworm main" > /etc/apt/sources.list.d/ceph.list; \
    apt-get update; \
    apt-get install -y --no-install-recommends ceph-common iproute2 nftables podman; \
    v="$(dpkg-query -W -f='${Version}' ceph-common)"; \
    if ! dpkg --compare-versions "$v" ge "${CEPH_MIN_VERSION}"; then \
        echo "ceph-common $v is older than ${CEPH_MIN_VERSION}" >&2; exit 1; \
    fi; \
    rm -rf /var/lib/apt/lists/*
COPY --from=build /out/storage-runner /usr/local/bin/storage-runner
ENV STATE_DIR=/var/lib/rackflow-storage-runner
ENTRYPOINT ["/usr/local/bin/storage-runner"]
