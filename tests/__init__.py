"""Test package.

An ``__init__.py`` here makes ``tests`` an importable package, so ``conftest``
is loaded exactly once, as ``tests.conftest``. Without it pytest inserts
``tests/`` on ``sys.path`` and imports the file as a top-level ``conftest``
module — which means ``from tests.conftest import NetworkAccessAttempted`` loads
a *second* copy of that module, and ``pytest.raises`` fails to recognise the
exception it raises on the identity check. Importing the shared helpers from one
module object is what makes :mod:`tests.fixtures` and the exception types usable
across the suite.
"""
