"""Test package.

It is a real package (not a bare folder) so ``pytest`` inserts the project root
into ``sys.path`` and ``from tests.conftest import ...`` resolves to the very
same module pytest already imported for its fixtures.
"""
