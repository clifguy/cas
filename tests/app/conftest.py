"""tests/app-specific fixtures.

The per-vault timing-resource leak guard that used to live here is now an
autouse fixture in the root ``tests/conftest.py`` (machinery in
``tests/helpers/timing_leaks.py``), so it covers every test tree, not just this
one. This module is the home for any future fixtures scoped specifically to the
app integration tests.
"""
