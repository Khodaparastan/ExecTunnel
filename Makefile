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
ENVACT				:= $(shell zsh -c "poetry env activate")
ENVDIR				:= $(shell zsh -c "dirname $(ENVACT) | tail -1")
PYTHON        := $(ENVDIR)/python
PIP           := $(ENVDIR)/pip
POETRY        := poetry
RUFF          := $(ENVDIR)/ruff
MYPY          := $(ENVDIR)/mypy
PYTEST        := $(ENVDIR)/pytest

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

.PHONY: run
run:
	$(PYTHON) -m exectunnel.cli run

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

# ── Pod-side helper (pod_echo) ───────────────────────────────────────────────
POD_ECHO_DIR := tools/pod_echo

.PHONY: build-pod-echo
build-pod-echo: ## Cross-compile pod_echo helper for linux/amd64 + linux/arm64
	$(MAKE) -C $(POD_ECHO_DIR) all GO=$(GO)
	@echo "$(GREEN)Built pod_echo helper (amd64 + arm64).$(RESET)"

.PHONY: clean-pod-echo
clean-pod-echo: ## Remove compiled pod_echo binaries
	$(MAKE) -C $(POD_ECHO_DIR) clean
	@echo "$(GREEN)Cleaned pod_echo binaries.$(RESET)"

# ── Benchmarks (measurement framework) ───────────────────────────────────────
# See docs/measurement.md for the conceptual overview and how to interpret
# the layered (L1..L4) reports.
BENCH_LABEL ?= baseline
BENCH_DURATION_SEC ?= 30
BENCH_PAYLOAD_SIZE ?= 65536
BENCH_HOST_KIND ?= laptop
BENCH_REPEATS ?= 3
BENCH_ENDPOINT ?=
BENCH_OUTPUT_DIR ?= ./bench-reports

.PHONY: bench-baseline
bench-baseline: ## Run layered bench (L1+L4) with the Python agent
	$(PYTHON) bench_layered.py \
		--label $(BENCH_LABEL) \
		--agent python \
		--bench-host-kind $(BENCH_HOST_KIND) \
		--duration-sec $(BENCH_DURATION_SEC) \
		--payload-size $(BENCH_PAYLOAD_SIZE) \
		--repeats $(BENCH_REPEATS) \
		--output-dir $(BENCH_OUTPUT_DIR) \
		$(if $(BENCH_ENDPOINT),--endpoint $(BENCH_ENDPOINT),)

.PHONY: bench-go
bench-go: ## Run layered bench with the Go agent (requires built go agent binary)
	$(PYTHON) bench_layered.py \
		--label $(BENCH_LABEL)-go \
		--agent go \
		--bench-host-kind $(BENCH_HOST_KIND) \
		--duration-sec $(BENCH_DURATION_SEC) \
		--payload-size $(BENCH_PAYLOAD_SIZE) \
		--repeats $(BENCH_REPEATS) \
		--output-dir $(BENCH_OUTPUT_DIR) \
		$(if $(BENCH_ENDPOINT),--endpoint $(BENCH_ENDPOINT),)

.PHONY: bench-compare
bench-compare: ## Diff two bench JSON reports — usage: make bench-compare A=path/a.json B=path/b.json
	@if [ -z "$(A)" ] || [ -z "$(B)" ]; then \
		echo "$(BOLD)usage:$(RESET) make bench-compare A=<path-to-a.json> B=<path-to-b.json>"; \
		exit 2; \
	fi
	$(PYTHON) bench_compare.py $(A) $(B)

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
