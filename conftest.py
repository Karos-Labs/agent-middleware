"""Root conftest — registers the real-Postgres fixtures as a plugin.

`pytest_plugins` is only honoured in the rootdir conftest (pytest 8 raises on
it anywhere else), and this is the one place it can live. The alternative was
importing the fixture names into the test module, which shadows the parameter
of every test that takes one -- the fixtures then look like redefinitions to
every linter, and to a reader.

`tests/conftest_postgres.py` is a plugin rather than part of
`tests/conftest.py` so that a suite run that never touches the Configuration
API never starts a database.
"""

pytest_plugins = ("tests.conftest_postgres",)
