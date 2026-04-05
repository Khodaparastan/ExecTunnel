"""kubectl discovery, kubeconfig parsing, and exec WebSocket URL construction."""

from __future__ import annotations

import asyncio
import base64
import json
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlencode, urlparse

import httpx
from exectunnel.exceptions import ConfigurationError

__all__ = [
    "KubectlContext",
    "PodSpec",
    "KubectlDiscovery",
    "build_exec_ws_url",
]


@dataclass(slots=True)
class KubectlContext:
    """Parsed kubeconfig context."""

    name:            str
    cluster_name:    str
    server:          str
    namespace:       str
    user:            str
    ca_data:         bytes | None       = None
    client_cert:     bytes | None       = None
    client_key:      bytes | None       = None
    token:           str | None         = None
    token_cmd:       list[str] | None   = None
    insecure_skip:   bool               = False


@dataclass(slots=True)
class PodSpec:
    """Resolved pod target for exec."""

    name:       str
    namespace:  str
    container:  str | None
    node:       str | None   = None
    phase:      str | None   = None
    ip:         str | None   = None
    labels:     dict[str, str] = field(default_factory=dict)
    conditions: list[dict[str, str]] = field(default_factory=list)


class KubectlDiscovery:
    """Discovers pods and builds exec WebSocket URLs from kubeconfig.

    Args:
        kubeconfig: Path to kubeconfig file.  Defaults to ``~/.kube/config``
                    or ``KUBECONFIG`` env var.
        context:    kubeconfig context name.  Defaults to current context.
    """

    __slots__ = ("_kubeconfig", "_context_name", "_ctx", "_token_cache")

    def __init__(
        self,
        kubeconfig: str | None = None,
        context: str | None = None,
    ) -> None:
        self._kubeconfig = kubeconfig or os.environ.get(
            "KUBECONFIG", str(Path.home() / ".kube" / "config")
        )
        self._context_name = context
        self._ctx: KubectlContext | None = None
        self._token_cache: str | None = None

    # ── Context loading ───────────────────────────────────────────────────────

    def load_context(self) -> KubectlContext:
        """Parse kubeconfig and return the active context."""
        if self._ctx is not None:
            return self._ctx

        kc_path = Path(self._kubeconfig)
        if not kc_path.exists():
            raise ConfigurationError(
                f"kubeconfig not found at {kc_path}",
                error_code="config.kubeconfig_not_found",
                details={"path": str(kc_path)},
                hint="Set KUBECONFIG or pass --kubeconfig.",
            )

        try:
            import yaml  # type: ignore[import-untyped]
            raw = yaml.safe_load(kc_path.read_text())
        except Exception as exc:
            raise ConfigurationError(
                f"Failed to parse kubeconfig: {exc}",
                error_code="config.kubeconfig_parse_error",
                details={"path": str(kc_path)},
            ) from exc

        ctx_name = self._context_name or raw.get("current-context", "")
        contexts = {c["name"]: c["context"] for c in raw.get("contexts", [])}
        clusters = {c["name"]: c["cluster"] for c in raw.get("clusters", [])}
        users    = {u["name"]: u["user"]    for u in raw.get("users", [])}

        if ctx_name not in contexts:
            available = ", ".join(sorted(contexts))
            raise ConfigurationError(
                f"Context {ctx_name!r} not found in kubeconfig.",
                error_code="config.kubeconfig_context_not_found",
                details={"context": ctx_name, "available": available},
                hint=f"Available contexts: {available}",
            )

        ctx_data     = contexts[ctx_name]
        cluster_name = ctx_data.get("cluster", "")
        cluster      = clusters.get(cluster_name, {})
        user_name    = ctx_data.get("user", "")
        user         = users.get(user_name, {})
        namespace    = ctx_data.get("namespace", "default")
        server       = cluster.get("server", "")

        # CA
        ca_data: bytes | None = None
        if "certificate-authority-data" in cluster:
            ca_data = base64.b64decode(cluster["certificate-authority-data"])
        elif "certificate-authority" in cluster:
            ca_data = Path(cluster["certificate-authority"]).read_bytes()

        # Client cert / key
        client_cert: bytes | None = None
        client_key:  bytes | None = None
        if "client-certificate-data" in user:
            client_cert = base64.b64decode(user["client-certificate-data"])
        elif "client-certificate" in user:
            client_cert = Path(user["client-certificate"]).read_bytes()
        if "client-key-data" in user:
            client_key = base64.b64decode(user["client-key-data"])
        elif "client-key" in user:
            client_key = Path(user["client-key"]).read_bytes()

        # Token / exec credential
        token: str | None = user.get("token")
        token_cmd: list[str] | None = None
        if "exec" in user:
            exec_cfg = user["exec"]
            token_cmd = [exec_cfg["command"]] + exec_cfg.get("args", [])

        self._ctx = KubectlContext(
            name=ctx_name,
            cluster_name=cluster_name,
            server=server,
            namespace=namespace,
            user=user_name,
            ca_data=ca_data,
            client_cert=client_cert,
            client_key=client_key,
            token=token,
            token_cmd=token_cmd,
            insecure_skip=cluster.get("insecure-skip-tls-verify", False),
        )
        return self._ctx

    async def resolve_token(self) -> str | None:
        """Resolve bearer token, executing credential plugin if needed."""
        ctx = self.load_context()
        if ctx.token:
            return ctx.token
        if not ctx.token_cmd:
            return None
        if self._token_cache:
            return self._token_cache

        loop = asyncio.get_running_loop()
        try:
            result = await loop.run_in_executor(
                None,
                lambda: subprocess.run(
                    ctx.token_cmd,
                    capture_output=True,
                    text=True,
                    timeout=10,
                    check=True,
                ),
            )
            cred = json.loads(result.stdout)
            token = (
                cred.get("status", {}).get("token")
                or cred.get("token")
            )
            self._token_cache = token
            return token
        except Exception as exc:
            raise ConfigurationError(
                f"Credential plugin failed: {exc}",
                error_code="config.credential_plugin_failed",
                details={"command": ctx.token_cmd[0]},
            ) from exc

    # ── Pod discovery ─────────────────────────────────────────────────────────

    async def list_pods(
        self,
        namespace: str | None = None,
        label_selector: str | None = None,
    ) -> list[PodSpec]:
        """List pods via the Kubernetes API."""
        ctx = self.load_context()
        ns = namespace or ctx.namespace
        token = await self.resolve_token()

        params: dict[str, str] = {}
        if label_selector:
            params["labelSelector"] = label_selector

        url = f"{ctx.server}/api/v1/namespaces/{ns}/pods"
        if params:
            url += "?" + urlencode(params)

        async with self._make_client(ctx, token) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            data = resp.json()

        pods = []
        for item in data.get("items", []):
            meta   = item.get("metadata", {})
            spec   = item.get("spec", {})
            status = item.get("status", {})
            containers = [c["name"] for c in spec.get("containers", [])]
            pods.append(
                PodSpec(
                    name=meta.get("name", ""),
                    namespace=meta.get("namespace", ns),
                    container=containers[0] if containers else None,
                    node=spec.get("nodeName"),
                    phase=status.get("phase"),
                    ip=status.get("podIP"),
                    labels=meta.get("labels", {}),
                    conditions=status.get("conditions", []),
                )
            )
        return pods

    async def get_pod(self, name: str, namespace: str | None = None) -> PodSpec:
        """Fetch a single pod by name."""
        ctx = self.load_context()
        ns = namespace or ctx.namespace
        token = await self.resolve_token()
        url = f"{ctx.server}/api/v1/namespaces/{ns}/pods/{name}"

        async with self._make_client(ctx, token) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            item = resp.json()

        meta   = item.get("metadata", {})
        spec   = item.get("spec", {})
        status = item.get("status", {})
        containers = [c["name"] for c in spec.get("containers", [])]
        return PodSpec(
            name=meta.get("name", ""),
            namespace=meta.get("namespace", ns),
            container=containers[0] if containers else None,
            node=spec.get("nodeName"),
            phase=status.get("phase"),
            ip=status.get("podIP"),
            labels=meta.get("labels", {}),
            conditions=status.get("conditions", []),
        )

    # ── URL construction ──────────────────────────────────────────────────────

    async def build_exec_url(
        self,
        pod: str,
        namespace: str | None = None,
        container: str | None = None,
        command: list[str] | None = None,
    ) -> tuple[str, dict[str, str]]:
        """Build the WebSocket exec URL and auth headers for a pod.

        Returns:
            ``(ws_url, headers)`` ready for ``websockets.connect()``.
        """
        ctx = self.load_context()
        ns = namespace or ctx.namespace
        cmd = command or ["/bin/sh"]
        token = await self.resolve_token()

        params: dict[str, Any] = {
            "stdout": "true",
            "stdin":  "true",
            "stderr": "true",
            "tty":    "false",
        }
        for c in cmd:
            params.setdefault("command", [])
            if isinstance(params["command"], list):
                params["command"].append(c)

        # Build query string with repeated "command" keys.
        qs_parts = []
        for k, v in params.items():
            if isinstance(v, list):
                for item in v:
                    qs_parts.append(f"{k}={item}")
            else:
                qs_parts.append(f"{k}={v}")
        if container:
            qs_parts.append(f"container={container}")

        api_path = (
            f"/api/v1/namespaces/{ns}/pods/{pod}"
            f"/exec?{'&'.join(qs_parts)}"
        )
        server = ctx.server.rstrip("/")
        ws_url = server.replace("https://", "wss://").replace(
            "http://", "ws://"
        ) + api_path

        headers: dict[str, str] = {}
        if token:
            headers["Authorization"] = f"Bearer {token}"

        return ws_url, headers

    # ── SSL context ───────────────────────────────────────────────────────────

    def make_ssl_context(self) -> Any:
        """Build an ssl.SSLContext from kubeconfig credentials."""
        import ssl
        import tempfile

        ctx = self.load_context()
        if ctx.insecure_skip:
            ssl_ctx = ssl.create_default_context()
            ssl_ctx.check_hostname = False
            ssl_ctx.verify_mode = ssl.CERT_NONE
            return ssl_ctx

        ssl_ctx = ssl.create_default_context()
        if ctx.ca_data:
            with tempfile.NamedTemporaryFile(delete=False, suffix=".crt") as f:
                f.write(ctx.ca_data)
                ca_path = f.name
            ssl_ctx.load_verify_locations(ca_path)
            os.unlink(ca_path)

        if ctx.client_cert and ctx.client_key:
            with (
                tempfile.NamedTemporaryFile(delete=False, suffix=".crt") as cf,
                tempfile.NamedTemporaryFile(delete=False, suffix=".key") as kf,
            ):
                cf.write(ctx.client_cert)
                kf.write(ctx.client_key)
                cert_path = cf.name
                key_path  = kf.name
            ssl_ctx.load_cert_chain(cert_path, key_path)
            os.unlink(cert_path)
            os.unlink(key_path)

        return ssl_ctx

    # ── httpx client ──────────────────────────────────────────────────────────

    def _make_client(
        self, ctx: KubectlContext, token: str | None
    ) -> httpx.AsyncClient:
        import ssl
        import tempfile

        verify: Any = True
        if ctx.insecure_skip:
            verify = False
        elif ctx.ca_data:
            with tempfile.NamedTemporaryFile(delete=False, suffix=".crt") as f:
                f.write(ctx.ca_data)
                verify = f.name

        headers: dict[str, str] = {}
        if token:
            headers["Authorization"] = f"Bearer {token}"

        cert: Any = None
        if ctx.client_cert and ctx.client_key:
            with (
                tempfile.NamedTemporaryFile(delete=False, suffix=".crt") as cf,
                tempfile.NamedTemporaryFile(delete=False, suffix=".key") as kf,
            ):
                cf.write(ctx.client_cert)
                kf.write(ctx.client_key)
                cert = (cf.name, kf.name)

        return httpx.AsyncClient(
            verify=verify,
            cert=cert,
            headers=headers,
            timeout=15.0,
        )


def build_exec_ws_url(
    server: str,
    pod: str,
    namespace: str,
    container: str | None,
    command: list[str],
) -> str:
    """Build a raw exec WebSocket URL without kubeconfig."""
    parts = ["stdout=true", "stdin=true", "stderr=true", "tty=false"]
    for c in command:
        parts.append(f"command={c}")
    if container:
        parts.append(f"container={container}")
    api_path = (
        f"/api/v1/namespaces/{namespace}/pods/{pod}"
        f"/exec?{'&'.join(parts)}"
    )
    base = server.rstrip("/").replace("https://", "wss://").replace(
        "http://", "ws://"
    )
    return base + api_path
