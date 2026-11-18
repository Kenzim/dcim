FROM golang:1.22-bookworm AS build
WORKDIR /src
COPY go.mod ./
COPY . .
ARG VERSION=0.1.0
RUN CGO_ENABLED=0 GOOS=linux go build -trimpath -ldflags="-s -w -X main.version=${VERSION}" -o /out/storage-runner ./cmd/storage-runner

# The runner changes host networking and drives Ceph and podman, so it runs as
# root with the host network; see README.md for the required flags.
FROM debian:bookworm-slim
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates ceph-common iproute2 nftables podman \
    && rm -rf /var/lib/apt/lists/*
COPY --from=build /out/storage-runner /usr/local/bin/storage-runner
ENV STATE_DIR=/var/lib/rackflow-storage-runner
ENTRYPOINT ["/usr/local/bin/storage-runner"]
