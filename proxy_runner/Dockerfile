FROM golang:1.22-bookworm AS build
WORKDIR /src
COPY go.mod ./
COPY . .
RUN CGO_ENABLED=0 GOOS=linux go build -trimpath -ldflags="-s -w" -o /out/proxy-runner ./cmd/proxy-runner

FROM gcr.io/distroless/static-debian12:nonroot
COPY --from=build /out/proxy-runner /proxy-runner
ENV PROXY_LISTEN=:8080
ENV HEALTH_LISTEN=:8081
EXPOSE 8080 8081
USER nonroot:nonroot
ENTRYPOINT ["/proxy-runner"]
