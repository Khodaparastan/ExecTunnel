"""Shared annotated types used across the config package.

Centralises ``Annotated`` type aliases so that constraints are defined
once and reused by :mod:`_mixin`, :mod:`_tunnel`, and :mod:`_overrides`.
"""

from __future__ import annotations

from typing import Annotated

from pydantic import Field

__all__ = [
    "NonNegFloat",
    "NonNegInt",
    "PortInt",
    "PosFloat",
    "PosInt",
]

# Port number: 1–65535 (0 is reserved/wildcard, excluded intentionally)
PortInt = Annotated[int, Field(ge=1, le=65_535, title="Port Number")]

# Non-negative float: 0.0 is valid (e.g. pace interval = 0 means no pacing)
NonNegFloat = Annotated[float, Field(ge=0.0, title="Non-Negative Float")]

# Non-negative integer: 0 is valid for disabling retry loops.
NonNegInt = Annotated[int, Field(ge=0, title="Non-Negative Integer")]

# Strictly positive float: timeouts, delays, intervals
PosFloat = Annotated[float, Field(gt=0.0, title="Positive Float")]

# Strictly positive integer: counts, capacities
PosInt = Annotated[int, Field(gt=0, title="Positive Integer")]
