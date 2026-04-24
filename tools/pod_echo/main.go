// pod_echo: a tiny pod-side helper used by the ExecTunnel measurement
// framework (bench_layered.py, layer L3).
//
// Three TCP servers bind to 127.0.0.1 and serve as deterministic,
// dependency-free traffic targets *inside* the pod so that layered
// decomposition can isolate the agent + transport path from external
// network variability:
//
//	echo   — reads bytes and writes them back unchanged (RTT / ping-pong)
//	source — streams a deterministic pseudo-random pattern (pure throughput, read)
//	sink   — reads and discards, counting bytes (pure throughput, write)
//
// The binary is delivered to the pod via the same base64-over-kubectl
// mechanism as the agent. It has zero runtime dependencies and is safe
// to run under `set -e` shells: it logs to stderr and exits 0 on
// SIGTERM / SIGINT.
//
// Build:
//
//	cd tools/pod_echo
//	make all   # produces ../../payload/pod_echo/pod_echo_linux_{amd64,arm64}
package main

import (
	"context"
	"encoding/binary"
	"errors"
	"flag"
	"fmt"
	"io"
	"log"
	"net"
	"os"
	"os/signal"
	"sync/atomic"
	"syscall"
	"time"
)

const (
	// Fixed PRNG seed so L3 measurements are comparable across runs.
	prngSeed uint64 = 0x9E3779B97F4A7C15

	// Buffer size for echo / sink reads. 64 KiB is a sensible default
	// that fills a typical TCP socket buffer in one syscall.
	ioBufSize = 64 * 1024

	// Chunk size for the source stream.
	sourceChunkSize = 64 * 1024
)

var (
	echoPort   = flag.Int("echo-port", 17001, "TCP port for the echo server")
	sourcePort = flag.Int("source-port", 17002, "TCP port for the source (read-from) server")
	sinkPort   = flag.Int("sink-port", 17003, "TCP port for the sink (write-to) server")
	bindAddr   = flag.String("bind", "127.0.0.1", "Address to bind all servers to")
	verbose    = flag.Bool("v", false, "Verbose logging (per-connection events)")
)

// ── deterministic PRNG (xorshift64*) ─────────────────────────────────────────

type xorshift64 struct{ s uint64 }

func newXorshift64(seed uint64) *xorshift64 {
	if seed == 0 {
		seed = 1
	}
	return &xorshift64{s: seed}
}

func (r *xorshift64) next() uint64 {
	x := r.s
	x ^= x >> 12
	x ^= x << 25
	x ^= x >> 27
	r.s = x
	return x * 0x2545F4914F6CDD1D
}

// fill fills buf with deterministic bytes. The PRNG state advances so
// subsequent calls produce distinct bytes.
func (r *xorshift64) fill(buf []byte) {
	i := 0
	for i+8 <= len(buf) {
		binary.LittleEndian.PutUint64(buf[i:], r.next())
		i += 8
	}
	if i < len(buf) {
		var tail [8]byte
		binary.LittleEndian.PutUint64(tail[:], r.next())
		copy(buf[i:], tail[:])
	}
}

// ── server helpers ───────────────────────────────────────────────────────────

func listen(ctx context.Context, addr string, role string, handler func(ctx context.Context, conn net.Conn)) error {
	var lc net.ListenConfig
	ln, err := lc.Listen(ctx, "tcp", addr)
	if err != nil {
		return fmt.Errorf("%s: listen %s: %w", role, addr, err)
	}
	log.Printf("%s: listening on %s", role, ln.Addr())

	go func() {
		<-ctx.Done()
		_ = ln.Close()
	}()

	go func() {
		for {
			conn, err := ln.Accept()
			if err != nil {
				if ctx.Err() != nil {
					return
				}
				if errors.Is(err, net.ErrClosed) {
					return
				}
				log.Printf("%s: accept error: %v", role, err)
				// Brief backoff on transient errors.
				time.Sleep(10 * time.Millisecond)
				continue
			}
			if *verbose {
				log.Printf("%s: connection from %s", role, conn.RemoteAddr())
			}
			go func(c net.Conn) {
				defer c.Close()
				handler(ctx, c)
			}(conn)
		}
	}()
	return nil
}

// ── echo ─────────────────────────────────────────────────────────────────────

func handleEcho(ctx context.Context, conn net.Conn) {
	_ = ctx
	buf := make([]byte, ioBufSize)
	for {
		n, err := conn.Read(buf)
		if n > 0 {
			if _, werr := conn.Write(buf[:n]); werr != nil {
				return
			}
		}
		if err != nil {
			return
		}
	}
}

// ── source ───────────────────────────────────────────────────────────────────

func handleSource(ctx context.Context, conn net.Conn) {
	_ = ctx
	rng := newXorshift64(prngSeed)
	buf := make([]byte, sourceChunkSize)
	for {
		rng.fill(buf)
		if _, err := conn.Write(buf); err != nil {
			return
		}
	}
}

// ── sink ─────────────────────────────────────────────────────────────────────

func handleSink(ctx context.Context, conn net.Conn) {
	_ = ctx
	var total uint64
	buf := make([]byte, ioBufSize)
	for {
		n, err := conn.Read(buf)
		if n > 0 {
			atomic.AddUint64(&total, uint64(n))
		}
		if err != nil {
			if *verbose && !errors.Is(err, io.EOF) {
				log.Printf("sink: conn closed after %d bytes: %v", atomic.LoadUint64(&total), err)
			}
			return
		}
	}
}

// ── main ─────────────────────────────────────────────────────────────────────

func main() {
	flag.Parse()
	log.SetFlags(log.LstdFlags | log.Lmicroseconds)
	log.SetOutput(os.Stderr)

	ctx, cancel := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer cancel()

	addr := func(port int) string { return fmt.Sprintf("%s:%d", *bindAddr, port) }

	if err := listen(ctx, addr(*echoPort), "echo", handleEcho); err != nil {
		log.Fatalf("echo: %v", err)
	}
	if err := listen(ctx, addr(*sourcePort), "source", handleSource); err != nil {
		log.Fatalf("source: %v", err)
	}
	if err := listen(ctx, addr(*sinkPort), "sink", handleSink); err != nil {
		log.Fatalf("sink: %v", err)
	}

	log.Printf("pod_echo ready: echo=%d source=%d sink=%d", *echoPort, *sourcePort, *sinkPort)
	<-ctx.Done()
	log.Printf("pod_echo: shutting down")
	// Small grace period for in-flight handlers.
	time.Sleep(50 * time.Millisecond)
}
