# ThinkParse — single entry point for local development and CI/CD.
# Every command the pipeline runs lives here; .github/workflows/ci.yml only
# calls these targets. Run `make help` to see everything.

SHELL := /bin/sh
.DEFAULT_GOAL := help
.PHONY: help install install-mcp lint lint-mcp lint-fix test test-mcp \
        test-mcp-integration ci docker-build-control docker-build-mcp \
        docker-build-mineru-cpu docker-build-mineru-gpu env compose-up \
        compose-down compose-logs compose-ps clean

PYTHON := $(if $(wildcard .venv/bin/python),.venv/bin/python,python3)
UV ?= uv
export PYTHONPATH := .

# Compose resolves .env from the compose file's directory (docker/), not the
# repo root, so --env-file points it back at the root .env explicitly.
# ENGINE overrides COMPOSE_PROFILES from .env:
#   cpu | gpu | gpu,gpu1 | "" (external MinerU, set MINERU_BASE_URLS in .env)
ENGINE ?= gpu
COMPOSE = COMPOSE_PROFILES=$(ENGINE) docker compose --env-file .env -f docker/docker-compose.yml

# Image tags mirror docker/docker-compose.yml.
CONTROL_IMAGE ?= thinkparse-control:2.0
MCP_IMAGE ?= thinkparse-mcp:0.1.0
MINERU_VERSION ?= 4.0.10
MINERU_GPU_TIER ?= standard

##@ Setup
install: ## Create .venv and install control-plane dev dependencies
	@test -x .venv/bin/python || python3 -m venv .venv
	.venv/bin/python -m pip install --upgrade pip
	.venv/bin/python -m pip install -r control/requirements-dev.txt

install-mcp: ## Install the thinkparse-mcp project into mcp/.venv
	$(UV) --directory mcp sync

##@ Quality
lint: ## Ruff-check the control plane and its tests
	$(PYTHON) -m ruff check control tests

lint-mcp: ## Ruff-check the MCP server sources and tests
	$(UV) --directory mcp run ruff check src tests

lint-fix: ## Auto-fix lint findings (both projects)
	$(PYTHON) -m ruff check --fix control tests
	$(UV) --directory mcp run ruff check --fix src tests

##@ Tests
test: ## Run the control-plane unittest suite (tests/test_*.py)
	$(PYTHON) -m unittest discover -s tests -p 'test_*.py'

test-mcp: ## Run the MCP server unit tests (integration tests skipped)
	$(UV) --directory mcp run pytest

test-mcp-integration: ## Run MCP integration tests against the stub gateway
	MCP_IT=1 $(UV) --directory mcp run pytest tests/integration

ci: lint test lint-mcp test-mcp ## Full local CI gate: lint and test everything

##@ Docker images
docker-build-control: ## Build the gateway/reconciler image
	docker build -f control/Dockerfile -t $(CONTROL_IMAGE) .

docker-build-mcp: ## Build the MCP server image
	docker build -t $(MCP_IMAGE) mcp

docker-build-mineru-cpu: ## Build the MinerU CPU image
	docker build -f docker/mineru-cpu.Dockerfile \
		--build-arg MINERU_VERSION=$(MINERU_VERSION) \
		-t thinkparse-mineru:$(MINERU_VERSION)-cpu docker

docker-build-mineru-gpu: ## Build the MinerU GPU image (MINERU_GPU_TIER=standard|basic)
	docker build -f docker/mineru.Dockerfile \
		--build-arg MINERU_VERSION=$(MINERU_VERSION) \
		--build-arg MINERU_GPU_TIER=$(MINERU_GPU_TIER) \
		-t thinkparse-mineru:$(MINERU_VERSION)-gpu-$(MINERU_GPU_TIER) docker

##@ Local stack (docker compose)
env: ## Create .env from .env.example if missing
	@test -f .env || { cp .env.example .env; \
		echo "Created .env from .env.example — set POSTGRES_PASSWORD before exposing the host."; }

compose-up: env ## Build and start the stack (make compose-up ENGINE=cpu)
	$(COMPOSE) up -d --build

compose-down: ## Stop the stack (volumes are kept)
	$(COMPOSE) down

compose-logs: ## Follow stack logs
	$(COMPOSE) logs -f

compose-ps: ## Show stack status
	$(COMPOSE) ps

##@ Maintenance
clean: ## Remove caches (keeps .venv directories)
	find . -path ./.venv -prune -o -path ./mcp/.venv -prune -o \
		-name '__pycache__' -type d -prune -exec rm -rf {} +
	rm -rf .pytest_cache mcp/.pytest_cache .ruff_cache mcp/.ruff_cache

help: ## Show this help
	@awk 'BEGIN {FS = ":.*##"} \
		/^##@/ {printf "\n\033[1m%s\033[0m\n", substr($$0, 5)} \
		/^[a-zA-Z_-]+:.*##/ {printf "  \033[36m%-22s\033[0m %s\n", $$1, $$2}' $(MAKEFILE_LIST)
