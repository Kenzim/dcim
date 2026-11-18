FROM golang:1.22-bookworm AS build
WORKDIR /src
COPY go.mod ./
COPY . .
RUN CGO_ENABLED=0 GOOS=linux go build -trimpath -ldflags="-s -w" -o /out/proxy-runner ./cmd/proxy-runner

# Proxy fast path image: needs ip and nft and runs as root. Select it with
# `target: fastpath`; the default image below is unchanged.
FROM debian:bookworm-slim AS fastpath
RUN apt-get update \
    && apt-get install -y --no-install-recommends iproute2 nftables \
    && rm -rf /var/lib/apt/lists/*
COPY --from=build /out/proxy-runner /proxy-runner
ENV PROXY_LISTEN=:11080
ENV HEALTH_LISTEN=:8081
EXPOSE 11080 8081
ENTRYPOINT ["/proxy-runner"]

FROM gcr.io/distroless/static-debian12:nonroot
COPY --from=build /out/proxy-runner /proxy-runner
ENV PROXY_LISTEN=:11080
ENV HEALTH_LISTEN=:8081
EXPOSE 11080 8081
USER nonroot:nonroot
ENTRYPOINT ["/proxy-runner"]
