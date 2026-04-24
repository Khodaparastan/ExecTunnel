# ExecTunnel

> **Alpha / Proof-of-Concept** — not production-ready. APIs and behaviour may change without notice.

SOCKS5 tunnel over PTY WEBSCOKET for restricted environments.

`ExecTunnel` exists for the real-world case where you do **not** get normal Kubernetes access (no kubeconfig, no namespace, no port-forward), but you **do** get a web terminal that internally uses an exec WebSocket stream.

## Why This Exists
Many managed platforms expose Kubernetes behind a dashboard/API with limited operator access:
- no direct cluster credentials
- web terminal only
- strict WAF/egress controls
- heavy limitations on traditional VPN or forwarding workflows

`ExecTunnel` turns that exec/WebSocket path into a local SOCKS5 bridge so you can stay in your own terminal and tooling.

## Intended Use
Use `ExecTunnel` when:
- you have authorized access to a platform-provided terminal/exec channel
- you need local tooling (CLI, browser, SDKs) to reach internal services
- standard approaches (kubectl, VPN, SSH tunnel) are unavailable or blocked

## Non-Goals
`ExecTunnel` is not trying to replace full service-mesh/dev-platform tools (e.g. Telepresence/mirrord) in open environments. It targets constrained operator scenarios.

## Features
- Local SOCKS5 (`CONNECT`, `UDP ASSOCIATE`)
- TCP/UDP relay over one framed WebSocket stream
- Optional DNS forwarding through tunnel (`--dns`)
- CIDR exclusions for local direct routing
- Auto-reconnect and ACK-timeout health recovery
- Structured observability (logs/metrics/tracing + exporters)

## Quick Start
### Local (Poetry)
```bash
poetry install
export WSS_URL='wss://your-endpoint.example/ws'
poetry run exectunnel tunnel
```

Route traffic:
```bash
export ALL_PROXY='socks5://127.0.0.1:1080'
```

### Docker

```bash
# Pull from Docker Hub
docker pull khodaparastan/exectunnel:latest

# — or pull from GitHub Container Registry —
docker pull ghcr.io/khodaparastan/exectunnel:latest

# — or build locally —
docker build -t exectunnel:latest .

# Single tunnel
WSS_URL='wss://your-endpoint.example/ws' docker compose up

# Multi-tunnel manager (configure .env first)
cp .env.manager .env   # edit with your WSS_URL_* entries
docker compose -f docker-compose.manager.yml up
```

### Kubernetes

```bash
# Edit the Secret with your WSS_URL
vim deploy/kubernetes/secret.yaml

# Apply all resources via Kustomize
kubectl apply -k deploy/kubernetes/

# Verify
kubectl -n exectunnel get pods

# Port-forward the SOCKS5 proxy to localhost
kubectl -n exectunnel port-forward svc/exectunnel 1080:1080

# Tear down
kubectl delete -k deploy/kubernetes/
```

> **Note:** Because this is an alpha release, pip will not install it by default.
> Use `pip install --pre exectunnel` to install pre-release versions.

## Development
```bash
poetry install
poetry run pytest -q
poetry run ruff check .
poetry run mypy exectunnel
```

### Versioning
The single source of truth for the version is `exectunnel/_version.py`.
`pyproject.toml` reads it via `poetry-dynamic-versioning` — do not edit the version in `pyproject.toml` directly.

## Performance & measurement

ExecTunnel ships a layered measurement framework for attributing cost to
specific subsystems (network vs. agent vs. base64 ceiling). See
[`docs/measurement.md`](docs/measurement.md) for the conceptual overview.

Quick start:

```bash
make bench-baseline BENCH_LABEL=my-baseline        # produces JSON + MD + CSV
make bench-compare A=reports/a.json B=reports/b.json
make build-pod-echo                                 # optional: build L3 helper binary
```

Reports land in `./bench-reports/` (gitignored).