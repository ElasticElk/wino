"""Fail CI if models drift from migrations.

`makemigrations --check` is also in the GitHub workflow. This pytest keeps
the same guard in the suite that runs with `--ds=wine_cellar.conf.test`,
including the `db` fixture so Django has a configured database.
"""

from io import StringIO

from django.core.management import call_command


def test_makemigrations_check(db):
    out = StringIO()
    try:
        call_command(
            "makemigrations",
            check=True,
            dry_run=True,
            interactive=False,
            stdout=out,
            stderr=out,
        )
    except SystemExit as exc:
        if exc.code not in (0, None):
            raise AssertionError(
                f"models have changes not reflected in a migration:\n{out.getvalue()}"
            ) from exc
