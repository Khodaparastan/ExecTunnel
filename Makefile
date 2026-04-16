# ExecTunnel — top-level Makefile
#
# Targets
# -------
#   venv                   Create virtual environment.
#   install                Install all runtime + dev dependencies via Poetry.
#   lint                   Run ruff linter.
#   format                 Auto-format code with ruff.
#   format-check           Check formatting without making changes.
#   typecheck              Run mypy type checker.
#   check                  Run all code quality checks.
#   test                   Run test suite.
#   test-cov               Run tests with coverage report.
#   test-fast              Run tests, stop on first failure.
#   build                  Build source distribution and wheel.
#   publish-test           Publish to TestPyPI.
#   publish                Publish to PyPI.
#   docker-build           Build Docker image.
#   docker-run             Run single tunnel in Docker.
#   docker-run-manager     Run multi-tunnel manager in Docker.
#   docker-down            Stop and remove Docker containers.
#   k8s-apply              Apply Kubernetes manifests via Kustomize.
#   k8s-delete             Delete Kubernetes resources.
#   build-go-agent         Cross-compile the Go agent for Linux/amd64 (pod deployment).
#   build-go-agent-local   Compile the Go agent for the local OS/arch (testing only).
#   clean-go-agent         Remove compiled Go agent binaries.
#   clean                  Remove build artefacts and caches.
#   clean-venv             Remove virtual environment as well.
#   help                   Print this help.

PYTHON        := .venv/bin/python
PIP           := .venv/bin/pip
POETRY        := poetry
RUFF          := .venv/bin/ruff
MYPY          := .venv/bin/mypy
PYTEST        := .venv/bin/pytest

SRC_DIR       := exectunnel
TEST_DIR      := tests
DIST_DIR      := dist

GO            ?= go
GO_DIR        := exectunnel/payload/go_agent
GO_OUT        := $(shell pwd)/$(GO_DIR)/agent_linux_amd64
GO_OUT_LOCAL  := $(shell pwd)/$(GO_DIR)/agent

# ── Colours ──────────────────────────────────────────────────────────────────
BOLD  := \033[1m
RESET := \033[0m
GREEN := \033[32m
CYAN  := \033[36m

.DEFAULT_GOAL := help

.PHONY: help
help: ## Show this help message
	@echo ""
	@echo "  $(BOLD)ExecTunnel$(RESET) — available targets"
	@echo ""
	@awk 'BEGIN {FS = ":.*##"} /^[a-zA-Z_-]+:.*##/ { printf "  $(CYAN)%-20s$(RESET) %s\n", $$1, $$2 }' $(MAKEFILE_LIST)
	@echo ""

# ── Environment ──────────────────────────────────────────────────────────────
.PHONY: venv
venv: ## Create virtual environment
	python3 -m venv .venv
	$(PIP) install --upgrade pip

.PHONY: install
install: ## Install all runtime + dev dependencies via Poetry
	$(POETRY) install --with dev

.PHONY: install-pip
install-pip: ## Install dependencies directly with pip (no Poetry)
	$(PIP) install -e ".[dev]"

# ── Code Quality ─────────────────────────────────────────────────────────────
.PHONY: lint
lint: ## Run ruff linter
	$(RUFF) check $(SRC_DIR)

.PHONY: format
format: ## Auto-format code with ruff
	$(RUFF) format $(SRC_DIR)

.PHONY: format-check
format-check: ## Check formatting without making changes
	$(RUFF) format --check $(SRC_DIR)

.PHONY: typecheck
typecheck: ## Run mypy type checker
	$(MYPY) $(SRC_DIR)

.PHONY: check
check: lint format-check typecheck ## Run all code quality checks

# ── Tests ─────────────────────────────────────────────────────────────────────
.PHONY: test
test: ## Run test suite
	$(PYTEST) $(TEST_DIR) -v

.PHONY: test-cov
test-cov: ## Run tests with coverage report
	$(PYTEST) $(TEST_DIR) -v \
		--cov=$(SRC_DIR) \
		--cov-report=term-missing \
		--cov-report=html:htmlcov

.PHONY: test-fast
test-fast: ## Run tests, stop on first failure
	$(PYTEST) $(TEST_DIR) -x -q

# ── Build & Publish ───────────────────────────────────────────────────────────
.PHONY: build
build: clean ## Build source distribution and wheel
	$(POETRY) build
	@echo "$(GREEN)Build artifacts in $(DIST_DIR)/$(RESET)"

.PHONY: publish-test
publish-test: build ## Publish to TestPyPI
	$(POETRY) publish --repository testpypi

.PHONY: publish
publish: build ## Publish to PyPI (requires PYPI_TOKEN env var or configured credentials)
	$(POETRY) publish

# ── Docker ────────────────────────────────────────────────────────────────────
DOCKER_IMAGE   ?= exectunnel
DOCKER_TAG     ?= latest

.PHONY: docker-build
docker-build: ## Build Docker image
	docker build -t $(DOCKER_IMAGE):$(DOCKER_TAG) .

.PHONY: docker-run
docker-run: ## Run single tunnel in Docker (set WSS_URL in env or .env)
	docker compose up

.PHONY: docker-run-manager
docker-run-manager: ## Run multi-tunnel manager in Docker (uses .env)
	docker compose -f docker-compose.manager.yml up

.PHONY: docker-down
docker-down: ## Stop and remove Docker containers
	docker compose down
	docker compose -f docker-compose.manager.yml down 2>/dev/null || true

# ── Kubernetes ────────────────────────────────────────────────────────────────
K8S_DIR := deploy/kubernetes

.PHONY: k8s-apply
k8s-apply: ## Apply Kubernetes manifests via Kustomize
	kubectl apply -k $(K8S_DIR)

.PHONY: k8s-delete
k8s-delete: ## Delete Kubernetes resources
	kubectl delete -k $(K8S_DIR)

# ── Go Agent ──────────────────────────────────────────────────────────────────
.PHONY: build-go-agent
build-go-agent: ## Cross-compile Go agent for Linux/amd64 (pod deployment)
	cd $(GO_DIR) && CGO_ENABLED=0 GOOS=linux GOARCH=amd64 $(GO) build \
		-ldflags="-s -w" \
		-o $(GO_OUT) \
		.
	@echo "$(GREEN)Built: $(GO_OUT)$(RESET)"

.PHONY: build-go-agent-local
build-go-agent-local: ## Compile Go agent for local OS/arch (testing only)
	cd $(GO_DIR) && $(GO) build -o $(GO_OUT_LOCAL) .
	@echo "$(GREEN)Built local: $(GO_OUT_LOCAL)$(RESET)"

.PHONY: clean-go-agent
clean-go-agent: ## Remove compiled Go agent binaries
	rm -f $(GO_OUT) $(GO_OUT_LOCAL)
	@echo "$(GREEN)Cleaned Go agent binaries.$(RESET)"

# ── Housekeeping ──────────────────────────────────────────────────────────────
.PHONY: clean
clean: ## Remove build artefacts and caches
	rm -rf $(DIST_DIR) build *.egg-info $(SRC_DIR)/*.egg-info
	find . -type d -name __pycache__ -not -path './.venv/*' -exec rm -rf {} + 2>/dev/null || true
	find . -type f -name "*.pyc"     -not -path './.venv/*' -delete 2>/dev/null || true
	find . -type d -name ".mypy_cache" -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name ".ruff_cache"  -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name "htmlcov"      -exec rm -rf {} + 2>/dev/null || true
	find . -type f -name ".coverage"    -delete 2>/dev/null || true
	@echo "$(GREEN)Clean!$(RESET)"

.PHONY: clean-venv
clean-venv: clean ## Remove virtual environment as well
	rm -rf .venv

.PHONY: all
all: install check test build ## Install, check, test, and build
