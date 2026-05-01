"""Integration tier — tests that spawn subprocesses or open real sockets.

Every test in this package is gated behind ``@pytest.mark.integration``
(see ``[tool.pytest.ini_options].markers`` in ``pyproject.toml``) and is
deselected from the fast unit-tier run by ``make test-unit``.

Run only this tier with::

    make test-integration

or, equivalently::

    pytest -m integration tests/integration/
"""
