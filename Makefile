# Rackflow / DCIM – build Docker images
# Use: make help

.PHONY: help frontend docker-app docker-poller docker-dhcp-runner docker-tftp-runner docker-media-runner build clean

APP_IMAGE = dcim-app
POLLER_IMAGE = dcim-bandwidth-poller
DHCP_RUNNER_IMAGE = dcim-dhcp-runner
TFTP_RUNNER_IMAGE = dcim-tftp-runner
MEDIA_RUNNER_IMAGE = dcim-media-runner

help:
	@echo "Targets:"
	@echo "  frontend         Build frontend locally (output: frontend/dist)"
	@echo "  docker-app       Build app image (FastAPI + static frontend)"
	@echo "  docker-poller    Build bandwidth-poller image"
	@echo "  docker-dhcp-runner  Build dhcp-runner image"
	@echo "  docker-tftp-runner  Build tftp-runner image"
	@echo "  docker-media-runner Build media-runner image (HTTP + SMB ISOs)"
	@echo "  build            Build all images"
	@echo "  clean            Remove frontend dist and Python caches"

# --- Frontend (local) ---
frontend:
	cd frontend && npm ci && npm run build

# --- Docker images ---
docker-app:
	docker build --target app -t $(APP_IMAGE) .

docker-poller:
	docker build --target bandwidth-poller -t $(POLLER_IMAGE) .

docker-dhcp-runner:
	docker build -f dhcp_runner/Dockerfile -t $(DHCP_RUNNER_IMAGE) .

docker-tftp-runner:
	docker build -f tftp_runner/Dockerfile -t $(TFTP_RUNNER_IMAGE) .

docker-media-runner:
	docker build -f media_runner/Dockerfile -t $(MEDIA_RUNNER_IMAGE) .

build: docker-app docker-poller docker-dhcp-runner docker-tftp-runner docker-media-runner

# --- Clean ---
clean:
	rm -rf frontend/dist
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name .pytest_cache -exec rm -rf {} + 2>/dev/null || true
