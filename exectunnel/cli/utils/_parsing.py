"""Parsing helpers reused by CLI commands."""

from __future__ import annotations

import os
import re
from ipaddress import IPv4Address, IPv6Address, ip_address
from pathlib import Path

import typer
from pydantic import AnyHttpUrl, AnyWebsocketUrl, TypeAdapter, ValidationError

__all__ = [
    "parse_ip",
    "parse_wss_url",
    "parse_http_url",
    "parse_existing_file",
    "parse_ws_headers",
]

_WSS_URL_ADAPTER: TypeAdapter[AnyWebsocketUrl] = TypeAdapter(AnyWebsocketUrl)
_HTTP_URL_ADAPTER: TypeAdapter[AnyHttpUrl] = TypeAdapter(AnyHttpUrl)
_HEADER_NAME_RE = re.compile(r"^[!#$%&'*+\-.^_`|~0-9A-Za-z]+$", re.ASCII)


def _validate_header_pair(key: str, value: str, *, param_hint: str) -> None:
    if not _HEADER_NAME_RE.fullmatch(key):
        raise typer.BadParameter(
            f"Invalid header name {key!r}.",
            param_hint=param_hint,
        )
    if any(ch in value for ch in ("\r", "\n", "\x00")):
        raise typer.BadParameter(
            f"Invalid header value for {key!r}: CR, LF, and NUL are not allowed.",
            param_hint=param_hint,
        )


def parse_ip(raw: str | None, *, param_hint: str) -> IPv4Address | IPv6Address | None:
    if raw is None:
        return None
    try:
        return ip_address(raw)
    except ValueError:
        raise typer.BadParameter(
            f"{raw!r} is not a valid IPv4 or IPv6 address.",
            param_hint=param_hint,
        ) from None


def parse_wss_url(raw: str, *, param_hint: str) -> AnyWebsocketUrl:
    try:
        return _WSS_URL_ADAPTER.validate_python(raw)
    except ValidationError as exc:
        raise typer.BadParameter(
            f"{raw!r} is not a valid WebSocket URL: {exc}",
            param_hint=param_hint,
        ) from exc


def parse_http_url(raw: str | None, *, param_hint: str) -> AnyHttpUrl | None:
    if raw is None:
        return None
    try:
        return _HTTP_URL_ADAPTER.validate_python(raw)
    except ValidationError as exc:
        raise typer.BadParameter(
            f"{raw!r} is not a valid HTTP URL: {exc}",
            param_hint=param_hint,
        ) from exc


def parse_existing_file(raw: str | None, *, param_hint: str) -> Path | None:
    if raw is None:
        return None
    path = Path(raw).expanduser().resolve(strict=False)
    if not path.is_file():
        raise typer.BadParameter(
            f"{str(path)!r} does not exist or is not a file.",
            param_hint=param_hint,
        )
    return path


def parse_ws_headers(values: list[str]) -> dict[str, str]:
    """Parse ``--ws-header`` entries plus optional env token."""
    headers: dict[str, str] = {}
    seen_keys: dict[str, str] = {}

    token = os.environ.get("EXECTUNNEL_TOKEN")
    if token:
        _validate_header_pair(
            "Authorization", f"Bearer {token}", param_hint="EXECTUNNEL_TOKEN"
        )
        headers["Authorization"] = f"Bearer {token}"
        seen_keys["authorization"] = "Authorization"

    for raw in values:
        key, sep, value = raw.partition(":")
        if not sep:
            raise typer.BadParameter(
                f"Invalid --ws-header {raw!r}: expected 'Key: Value' format.",
                param_hint="--ws-header",
            )

        normalized_key = key.strip()
        normalized_value = value.strip()

        if not normalized_key:
            raise typer.BadParameter(
                f"Invalid --ws-header {raw!r}: header name must not be empty.",
                param_hint="--ws-header",
            )

        _validate_header_pair(
            normalized_key,
            normalized_value,
            param_hint="--ws-header",
        )

        duplicate_key = normalized_key.casefold()
        if duplicate_key in seen_keys:
            raise typer.BadParameter(
                f"Duplicate --ws-header for {normalized_key!r}. "
                f"Header names are case-insensitive.",
                param_hint="--ws-header",
            )

        headers[normalized_key] = normalized_value
        seen_keys[duplicate_key] = normalized_key

    return headers
