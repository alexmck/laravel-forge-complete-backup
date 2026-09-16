# Development

Contributor setup and automated tests live here. For installation, configuration,
scheduling and recovery, see [README.md](README.md).

## Set up a development environment

Use Python 3.10+ on Linux or macOS. From the repository root:

```bash
python3 -m venv venv
venv/bin/python -m pip install -r requirements.txt
```

The tests use Python's standard-library `unittest` runner. They do not require a
production `config.yaml`, R2 credentials, a running database or a Discord webhook.

## Run unit tests

```bash
venv/bin/python -m unittest discover -s tests -v
```

Without `RESTIC_TEST_BINARY`, the real-restic integration test is skipped. Unit
tests cover database parsing and credential handling, dump validation, failure
propagation, retention filters, maintenance state, process timeouts, locking,
disk-space guards, installer checksums and Discord payloads. Discord requests are
mocked; the tests do not send messages.

Guided-setup tests cover configuration prompts, private file creation, preservation
of existing passwords, repository initialization decisions, and cron updates.
Storage calls and crontab writes are mocked in those tests.
Site-discovery tests cover standard and isolated layouts, stable deployment paths,
Nginx roots, permission notices, site selection and ambiguous database settings.
Dependency checks are tested with missing Python modules, optional system tools,
existing virtual environments and externally managed restic binaries.
Management tests cover site/settings preservation, retention overrides, private
rollback files, schedule updates and recovery-guide credential exclusion.

## Run the local restic integration test

Install the pinned restic binary, then run the suite with its absolute path:

```bash
python3 scripts/install_restic.py
RESTIC_TEST_BINARY="$PWD/bin/restic" venv/bin/python -m unittest discover -s tests -v
```

The installer downloads the binary and checksums from the official GitHub release.
The integration test itself uses a disposable local repository and generated fixture
files. It performs two backups, checks retention and exclusions, restores files and
a SQL fixture, and exercises pruning and repository integrity checks. Temporary
files and the repository are removed when the test finishes.

The database dump is mocked in this integration test. It verifies storage and
recovery of the SQL file, not a real MySQL/MariaDB dump or import. It also does not
exercise R2 connectivity, Forge file permissions or production resource usage.

## Check syntax and patch formatting

```bash
venv/bin/python -m py_compile backup.py database.py restic_backend.py runtime.py notifications.py site_discovery.py recovery.py scripts/check_dependencies.py scripts/install_restic.py scripts/setup.py
bash -n install.sh
git diff --check
```
