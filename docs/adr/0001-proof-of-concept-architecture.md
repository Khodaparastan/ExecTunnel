# ADR 0001 — Proof of Concept Architecture

## Status

Accepted

## Context

Many managed Kubernetes platforms expose cluster access only through a web-based
terminal (dashboard/API) that internally uses an exec WebSocket stream. In these
restricted environments:

- No direct cluster credentials (kubeconfig) are available.
- `kubectl port-forward` and similar tools are unavailable.
- Strict WAF, egress controls, or network policies block traditional VPN or
  forwarding workflows.
- The only reachable channel into the pod is the exec session's stdin/stdout.

ExecTunnel exists to provide TCP and UDP connectivity into such clusters by
piggy-backing on the exec WebSocket channel — the one path that is already
permitted.

To achieve this, the system is divided into three logical components:

- **Agent** — runs inside the Kubernetes pod.
- **Tunneling Service** — acts as an intermediary between the client-facing service and
  the in-cluster agent.
- **SOCKS5 Service** — provides a standard proxy interface for client applications.

This structure allows clients to route traffic through a familiar SOCKS5 interface while
abstracting the internal transport mechanism used to communicate with the cluster.

## Decision

The system will be implemented using the following architecture:

- The **Agent** runs inside a pod and is responsible for:
    - Handling TCP and UDP relay to internal services.
    - Communicating with the tunneling service over the exec session's stdin/stdout.
    - Encoding and decoding payloads as required by the active protocol version.

- The **Tunneling Service** sits between the SOCKS5 service and the agent and is
  responsible for:
    - Forwarding traffic between the client and the agent.
    - Maintaining the persistent exec session to the agent.
    - Managing connection multiplexing and lifecycle.
    - Handling session health monitoring.

- The **SOCKS5 Service** provides the client-facing interface:
    - Accepts SOCKS5 connections from local tools and applications.
    - Forwards the traffic through the tunneling service.
    - Does not implement HTTP/HTTPS proxy semantics.

This architecture isolates client protocol handling (SOCKS5), transport tunneling, and
in-cluster networking responsibilities.

### Component Interaction

```
┌──────────┐     ┌──────────────┐     ┌─────────────┐     ┌───────────────┐
│  Client  │────▶│ SOCKS5       │────▶│ Tunneling   │────▶│ Agent         │
│  App     │◀────│ Service      │◀────│ Service     │◀────│ (in-pod)      │
└──────────┘     └──────────────┘     └─────────────┘     └───────────────┘
                                            │
                                     K8s exec session
                                     (stdin/stdout)
```

## Consequences

- The system **does not implement an L3 network tunnel** (e.g., TUN/TAP or VPN-style
  networking).
- Traffic is proxied strictly through the **SOCKS5 protocol** at Layer 4.
- **HTTP/HTTPS proxy support is not implemented**.
- The multi-component architecture increases flexibility but adds operational
  complexity.
- Session resilience depends on exec session stability.

