"""ExecTunnel CLI package."""

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ._app import app as app


def __getattr__(name: str) -> Any:
    if name == "app":
        from ._app import app  # noqa: PLC0415

        return app
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = ["app"]
