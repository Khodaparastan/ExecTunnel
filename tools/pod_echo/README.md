# pod_echo

A tiny pod-side helper used by the ExecTunnel measurement framework
(`bench_layered.py`, layer **L3**). It provides three TCP servers bound
to `127.0.0.1` inside the pod:

| Role   | Default port | Behavior                                                      |
|--------|--------------|---------------------------------------------------------------|
| echo   | `17001`      | Reads bytes and writes them back unchanged (RTT / ping-pong). |
| source | `17002`      | Streams a deterministic xorshift64 pattern (read-throughput). |
| sink   | `17003`      | Reads and discards, counting bytes (write-throughput).        |

The binary is delivered to the pod through the same base64-over-kubectl
mechanism as the agent (see `exectunnel.session._payload.load_pod_echo_b64`).

## Build

```bash
cd tools/pod_echo
make all   # produces exectunnel/payload/pod_echo/pod_echo_linux_{amd64,arm64}
```

Or from the repository root:

```bash
make build-pod-echo
```

Individual targets:

```bash
make amd64
make arm64
make clean
```

The build is fully static (`CGO_ENABLED=0`) and stripped (`-s -w -trimpath`).

## Run (locally, for testing)

```bash
go run . -v
# pod_echo ready: echo=17001 source=17002 sink=17003
```

Custom ports:

```bash
go run . -echo-port=20001 -source-port=20002 -sink-port=20003
```

## Signals

`SIGINT` / `SIGTERM` trigger a graceful shutdown (closes listeners, drains
in-flight handlers for 50 ms, exits 0).

## Why a binary (not a shell one-liner)?

- Deterministic PRNG makes L3 throughput comparisons reproducible across
  runs — no `/dev/urandom` variance, no `openssl` dependency.
- Single static binary, zero runtime dependencies: works in minimal
  distroless / scratch pods.
- Uniform behavior on `linux/amd64` and `linux/arm64`.
