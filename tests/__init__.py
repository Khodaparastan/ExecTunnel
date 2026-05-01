"""Top-level test package for ExecTunnel.

Pytest does not strictly require this file (it can collect rootless test
trees), but having it pinned avoids ambiguous import-mode behaviour when
running individual files (``pytest tests/unit/foo.py``) and lets editors
resolve relative imports from sibling test helpers (e.g. ``tests._helpers``).

The package is intentionally empty apart from this docstring; shared
fixtures live in :mod:`tests.conftest` (autouse, project-wide), and
shared helpers live under :mod:`tests._helpers`.
"""
