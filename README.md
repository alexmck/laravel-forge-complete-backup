# Laravel Forge Complete Backup

Back up configured Forge site directories and MySQL/MariaDB databases to an encrypted
restic repository, including S3-compatible storage such as Cloudflare R2. Python
processes sites sequentially and sends per-site and summary Discord notifications.
No Forge API or particular Forge plan is required.

## What counts as success

A database-enabled site succeeds only after its SQL dump exits successfully, is
nonempty, has a completion footer, and its files and SQL are saved in a successful
restic snapshot. Partial snapshots (including restic exit 3) never receive the
`complete` tag. Backup, retention, or scheduled maintenance failures return a
nonzero process status. Other sites continue after a site fails.

A retention failure is reported separately: the new backup still exists. Discord
delivery failure is logged, but does not invalidate a backup. Disabling success
notifications never disables failure notifications.

Discord notifications use compact embeds with green success, amber warnings and
red failures. Site cards show the server, elapsed time, database validation/size,
retention policy and a copyable snapshot ID. Failures show the stage and reason.
Run summaries show successful/failed counts and identify sites needing attention.
Mentions are disabled, and large summaries are shortened to fit Discord's limits.
Existing global/per-site success controls and the summary toggle still apply.

## Install on Ubuntu / Forge

1. Have these ready:
   - An existing R2 bucket, its account ID, and an R2 API access key/secret with
     read/write/delete access to the bucket. Other S3-compatible HTTPS endpoints
     are also supported.
   - A repository password saved in your password manager. Keep it and the
     repository address outside the VPS: **losing every copy of the password makes
     the encrypted backups unrecoverable**.
   - Optionally a Discord webhook URL. The wizard detects common Forge site paths.

2. SSH in as the account that will run backups, then run:

   ```bash
   git clone https://github.com/alexmck/laravel-forge-complete-backup.git
   cd laravel-forge-complete-backup
   bash install.sh
   ```

   ![Example setup wizard detecting Laravel, WordPress and static sites](docs/images/setup-wizard.png)

   *Illustrative terminal preview using actual wizard output and fictional sites.*

3. Follow the prompts. The installer:
   - Checks Python support, existing virtual environments, database dump tools and
     cron before installing dependencies or asking for credentials.
   - Installs Python dependencies and a checksum-verified restic binary.
   - Asks for storage credentials and the saved repository password. Secret inputs
     are hidden.
   - Lists detected sites from `/home` and enabled Nginx configurations. Choose
     `all` (the default), numbers such as `1,3` or `1-3`, or `manual` to enter paths.
   - Detects database configuration and includes the database automatically when
     found. Ambiguous or unsupported settings require a selection or explicit
     files-only choice. Static sites without database configuration use files-only
     backups; other sites without detected settings prompt for confirmation.
   - Writes `config.yaml`, `restic.env` and `restic-password` with `0600` permissions.
   - Connects to an existing repository or initializes a missing one. Authentication
     and connection errors stop setup; they do not trigger initialization.
   - Generates a private `RECOVERY.md` with this installation's repository address,
     site filters and download/decryption commands. It contains no credentials.
   - Offers to run the first backup and install daily cron jobs for the current user.

Defaults are **7 daily / 4 weekly / 12 monthly** retention, sequential site backups,
and weekly repository maintenance. The proposed cron jobs run backups at **03:00**
and check for due maintenance at **05:00**, in the server's local time. Decline cron
installation if you prefer Forge's scheduler; the exact entries are printed.
Existing backup jobs outside the installer's managed block are not replaced.

Site discovery supports standard Forge directories, isolated users and `current`
deployment layouts. It inspects user directories and one site level beneath them,
plus literal Nginx `root` paths; it does not recursively search the server. Missing
sites can be added manually. Run as a user with access to the selected sites and
their database configuration; discovery does not grant permissions.

For an existing checkout, run `bash install.sh` there. Existing configuration,
passwords and the virtual environment are reused. An existing configuration opens
a management menu. Rerunning setup does not duplicate
its cron jobs. If a backup or connection fails, settings stay saved so you can
correct the problem and rerun. Legacy `global.s3` configuration requires the
[migration steps below](#migration-from-v021).

### Manage an existing installation

Run `venv/bin/python scripts/setup.py` to open the menu without reinstalling dependencies:

1. **Manage sites:** keep selected sites and discover or manually add new ones.
   Retained sites preserve their names, database settings, exclusions and overrides.
   Removing a site stops future backups; it does not delete existing snapshots.
2. **Change retention:** choose daily, weekly and monthly counts. Existing per-site
   overrides remain unless you explicitly remove them. Shorter retention can remove
   older snapshots after the next successful backup.
3. **Change scheduling:** enter daily backup and maintenance times in the server's
   local timezone, then optionally update the installer's managed cron block.
   If Forge manages your jobs, apply the printed entries there. Saving times alone
   does not change existing jobs. Allow enough time between backup and maintenance.
4. **Regenerate the recovery guide:** refresh `RECOVERY.md` without connecting to
   storage or running a backup.

Changes are confirmed before saving. The previous YAML is saved as private
`config.yaml.previous`; storage credential and password files are preserved.
YAML comments/formatting are not preserved when settings are saved. The rollback
copy contains configuration secrets, if any, and is ignored by Git.

Copy `RECOVERY.md` off the server and keep it alongside your repository password
in your password manager. It is generated locally, ignored by Git and updated when
the wizard saves settings. It includes infrastructure paths but no passwords,
access keys or Discord webhook. Recovery works without the original `restic.env`:
enter replacement storage credentials and the original repository password when
following the guide. Generating the guide does not itself test a restore.

### Prerequisites and Ubuntu versions

The backup account must be able to read all configured site files. Isolated Forge
site users may need group/ACL configuration; the installer does not change site
permissions. Python **3.10+**, its `venv` module, and a MySQL/MariaDB dump client for
database-enabled sites are required. Missing prerequisites produce an error with
next steps; the installer does not use sudo or change system packages.
The early check prints Ubuntu install commands for missing tools. Missing database
clients and cron are notices because files-only backups and Forge scheduling do
not require them. Database-enabled sites still require a working dump client.

| Ubuntu release | Default Python | Python requirement |
| --- | --- | --- |
| 24.04 LTS | 3.12 | Meets the requirement. |
| 22.04 LTS | 3.10 | Meets the requirement. |
| 20.04 LTS | 3.8 | Requires a separately installed Python 3.10+. |

Ubuntu 26.04 also meets the requirement. These describe prerequisites, not a claim
that each release has been tested. See Ubuntu's
[Python version reference](https://ubuntu.com/developers/docs/reference/availability/python/).

The installer defaults to `python3` on `PATH`. To select an existing newer Python:

```bash
PYTHON_BIN=/absolute/path/to/python3.12 bash install.sh
```

Do not replace Ubuntu's system Python. An existing virtual environment using an
older Python must be moved aside before creating one with the newer interpreter.

### Advanced installation and configuration

Use `bash install.sh --skip-setup` to install dependencies without interactive
configuration, or `venv/bin/python scripts/setup.py` to run only the setup prompts.
Use `--skip-restic` to manage the restic binary yourself; guided setup then uses an
existing project-local binary or `restic` on `PATH`. Options can be combined.

The installer downloads official restic **0.19.1** for Linux AMD64/ARM64 and checks
the release SHA-256 checksum over HTTPS. It does not depend on Ubuntu's restic
package version or automatically replace an existing binary. Restic 0.19.1+ is
required. To explicitly install the pinned version again:

```bash
python3 scripts/install_restic.py
```

Settings can be edited in `config.yaml` after setup. See `config.yaml.example` for
all settings and `restic.env.example` for storage credentials. For manual setup,
create these files and a `restic-password` file containing your saved password,
set their permissions to `0600`, and use absolute paths in the configuration.
Then run `venv/bin/python backup.py init` once for a new repository, followed by
`venv/bin/python backup.py backup`.

Each server should use its own stable repository prefix, such as
`s3:https://ACCOUNT.r2.cloudflarestorage.com/BUCKET/forge-server-1`. R2 uses region
`auto`. Do not enable S3 object-expiration lifecycle rules on a restic prefix;
restic manages retention itself.

The environment file accepts AWS access keys, session token, region, and
`RESTIC_PASSWORD_FILE`/`RESTIC_PASSWORD`. Shell commands and interpolation are not
evaluated. A configured password file takes precedence over a password variable.

## Resource usage

Sites run one at a time, with one restic file reader, one Go execution core and two
S3 connections. Files go directly to restic; there is no intermediate site copy or
`.tar.gz`. SQL is streamed to disk and removed after each site. Restic also uses
cache and temporary pack files. `work_dir` must be disk-backed, not tmpfs.

The default free-disk reserve is **1024 MiB**. It is checked before work, during SQL
writes and while restic runs. Crossing it aborts the operation; this is a guard,
not a filesystem quota or a guarantee against concurrent disk use by the site.
Allow enough additional space for the largest site's **uncompressed SQL dump**.
The cache has no fixed size cap. Do not lower the reserve simply to force a backup
onto a full server.

These settings reduce resource demand, but do not impose a hard RAM ceiling.
Large repositories may require checks/pruning on another machine or a larger VPS.
Full data checks download all repository data without creating a full local
restored copy.

## Database configuration

`backup_database: true` is the default. It must be explicitly false for files-only
sites. A missing client, ambiguous configuration, bad credentials or invalid dump
fails the site before restic runs.

Without an explicit `database.config_file`, runtime discovery requires exactly one
of these files under `user_path`:

- `.env`: `DB_HOST`, `DB_PORT`, `DB_DATABASE`, `DB_USERNAME`, `DB_PASSWORD`, optional
  `DB_SOCKET`; `DB_CONNECTION` must be `mysql` or `mariadb` when provided.
- `public/wp-config.php`: static `DB_*` definitions.
- `public/LocalSettings.php`: static `$wgDB*` assignments.
- `public/conf_global.php`: static `$INFO` assignments or array entries.

The wizard also checks site roots, `public`, `current` and shared configuration
locations, and saves an explicit `database.config_file`. For zero-downtime sites it
keeps the configuration path through `current` so it follows future deployments,
and selects the containing site directory with its releases and shared files.

When configuring another layout manually, set `database.config_file` to the
absolute path. Back up the containing site directory
that includes actual release/shared files: restic preserves symlinks, it does not
follow arbitrary links to files outside the source tree.

PHP is never executed. Dynamic expressions, interpolation and ambiguous sources
require an explicit override. `.env` quoting/comments are parsed with python-dotenv;
variable interpolation is intentionally disabled. Cached Laravel configuration may
differ from `.env`; choose explicit settings if `.env` is not authoritative.

Prefer an existing protected MySQL option file:

```yaml
database:
  name: application_db
  option_file: /home/forge/.mysql-backup.cnf
  dump_binary: mysqldump  # Use mariadb-dump for a MariaDB client.
  timeout_seconds: 1800
```

The option file must have permissions `0600` or `0400`, with a `[client]` section:

```ini
[client]
host="127.0.0.1"
port=3306
user="backup_user"
password="your password"
```

Alternatively set `database.name`, `user`, `password`, and optionally `host`, `port`
or `socket` in the private YAML. Supply all three credential fields to bypass
automatic discovery; an empty password is allowed. With `option_file`, credentials
come from that file and other credential overrides are ignored.

For discovered/inline credentials, the script writes a temporary `0600` option file
outside snapshot sources. Passwords never appear in process arguments. It disables
inherited `MYSQL_PWD`/login-file settings and uses `--defaults-file` as the first
option. Subprocess stderr is deliberately not copied into logs or Discord because
it can contain credentials or SQL. Failure messages include the operation and exit
status; investigate with the same client and protected option file when needed.

Dumps use `--single-transaction --quick --routines --triggers --events --hex-blob
--no-tablespaces`; MySQL also uses `--set-gtid-purged=OFF`. The database user needs
privileges to read tables/views and export routines, triggers and events. Dump
checks and SHA-256 metadata detect common incomplete output but do not validate SQL
import compatibility. Transactional consistency applies to transactional tables.
Avoid schema changes during dumps; files and SQL are not an atomic application-wide
snapshot. Nontransactional tables need separate coordination.

## Exclusions and retention

`exclude_patterns` are scoped to site files. Basename patterns such as `*.log` or
`node_modules` match at every depth; patterns containing `/` are relative to
`user_path`. Absolute paths, parent traversal and negation are rejected. SQL and
manifest files are explicit backup inputs, so site exclusions cannot remove them.
The work directory, restic secrets, explicit MySQL option file, and backup YAML are
excluded automatically. Application `.env` files remain part of encrypted backups.

Defaults are 7 daily, 4 weekly, and 12 monthly snapshots. Override any count with
`retention: {daily: 14}` per site; zero disables that tier, but all-zero retention
is rejected. Restic keeps the union of these policies, based on periods containing
backups, rather than an exact age cutoff. Retention runs only after a successful
site snapshot, filtered by `forge-backup`, server, site and `complete` tags and
grouped by host/tags. Keep names/server IDs stable to retain the same history.
While history is still filling up, restic can also preserve the oldest snapshot
for an unsatisfied retention tier.

Pruning is separate from snapshot retention. Default maintenance runs a structural
check and reads the next **1/4** of repository data every seven days, then prunes
with at most **128 MiB** of repacking. The subsets rotate only after successful
checks. This spreads verification across roughly four weeks; repository changes
mean it is not a single point-in-time full verification. Use `check` for a full read.
This repack limit bounds rewritten data, not memory. Failed checks skip pruning.
State is written atomically per repository; a failed operation stays due for retry.

Interrupted/partial snapshots lack `complete` and are deliberately outside automatic
retention. Inspect and explicitly forget their IDs after a successful replacement.
Do not blindly restore `latest` without the complete/site filters below.

## Schedule and operate

Configure two Forge scheduled jobs (or cron entries), using the same account and
absolute paths. Run maintenance daily so a failed weekly operation retries the
next day. The state file determines whether checks/pruning are due. Adjust hours
to leave enough time for your backups; all commands share a nonblocking local lock.
An overlapping job exits nonzero and sends a failure notification.

```cron
0 3 * * * /usr/bin/nice -n 10 /home/forge/laravel-forge-complete-backup/venv/bin/python /home/forge/laravel-forge-complete-backup/backup.py backup >> /home/forge/laravel-forge-complete-backup/cron.log 2>&1
0 5 * * * /usr/bin/nice -n 10 /home/forge/laravel-forge-complete-backup/venv/bin/python /home/forge/laravel-forge-complete-backup/backup.py maintenance >> /home/forge/laravel-forge-complete-backup/cron.log 2>&1
```

Use logrotate to bound the size of `cron.log`. Logs go to stderr; there is no duplicate
`backup.log`. SIGTERM/interrupts terminate child process groups and clean temporary
credentials/dumps where possible. OS locks release on exit. A hard kill can leave
staging files; each site's staging is cleared before its next backup and stale
generated credentials are cleared on startup. Do not manually delete the lock file.
Never run two installations for the same server with different work directories.

```bash
venv/bin/python backup.py maintenance  # Run only due maintenance.
venv/bin/python backup.py check        # Force a full repository read/check.
venv/bin/python backup.py --config /absolute/config.yaml backup
```

## Download, decrypt and verify a backup locally

Restic's `restore` command extracts ordinary, decrypted files into a folder. It does
not deploy the site, run PHP, connect to MySQL or import the SQL dump. There is no
single encrypted `.tar.gz` to download from R2; restic reconstructs the selected
snapshot from the repository's encrypted objects.

1. Install restic on your computer (`brew install restic` on macOS). Copy your
   `restic.env` and `restic-password` from the server into a private local directory,
   such as `$HOME/.config/forge-backups`, and set both files to mode `0600`. Load the
   credentials and use the same repository address as the server:

   ```bash
   set -a
   . "$HOME/.config/forge-backups/restic.env"
   set +a
   export RESTIC_REPOSITORY='s3:https://ACCOUNT.r2.cloudflarestorage.com/BUCKET/forge-server-1'
   export RESTIC_PASSWORD_FILE="$HOME/.config/forge-backups/restic-password"
   export AWS_DEFAULT_REGION=auto
   restic snapshots --tag 'forge-backup,server:forge-server-1,site:website1-com,complete'
   ```

2. Pick the exact snapshot ID (also shown in Discord). Download it to a new, empty
   local folder and verify the restored file contents:

   ```bash
   restic restore SNAPSHOT_ID --target "$HOME/forge-restore" --verify
   ```

   Absolute source paths are recreated underneath the target. The site appears at
   `$HOME/forge-restore/home/www-website1-com`. SQL and `manifest.json` appear
   under the restored original work directory, for example:
   `$HOME/forge-restore/home/forge/laravel-forge-complete-backup/.backup-work/staging/website1-com/`.
   The manifest records the database name, dump path, byte count and SHA-256.

3. Open the restored files and inspect `database.sql` in a text editor. A successful
   `restore --verify` confirms that the snapshot can be downloaded, decrypted and
   reconstructed with matching file contents. SQL remains a file; nothing is
   imported into a database. The local files are now plaintext, so keep the download
   folder private, especially its application `.env` files and SQL.

File verification checks restored contents, not whether MySQL can import the SQL.

### Recover a database or application

To recover a database, provision an **empty destination database** and a private
client option file, then import the downloaded SQL:

```bash
mysql --defaults-file="$HOME/.mysql-restore.cnf" restored_database < "$HOME/forge-restore/home/forge/laravel-forge-complete-backup/.backup-work/staging/website1-com/database.sql"
```

Use the compatible MariaDB client for MariaDB dumps. Database users/grants are not
included in a single-database dump. Confirm tables, application data and files
before switching production traffic to the recovered application. Production
cutover needs maintenance mode, correct file ownership/permissions, environment
settings and application checks. Restore on another machine if the VPS cannot
hold a second copy.

## Migration from v0.2.1

1. Keep the `v0.2.1` tag and existing `.tar.gz` objects. Pause the old scheduled job.
2. Install dependencies/restic. Replace `global.s3` with `global.restic`; move storage
   credentials into the protected environment file and create a repository password.
3. Replace `retention_days` with `retention.daily/weekly/monthly`; remove
   `compression_level`. The old value counted archives, not days. Review exclusion
   paths and database discovery. Preserve notification settings and site names.
4. Use a new restic prefix, initialize, run a backup, and enable both scheduled jobs.
   Existing archives are not imported or deleted by this system. Remove them only
   after separately deciding your migration retention period.

Rolling back the code does not convert restic snapshots into archives: restore the
old configuration and schedule alongside the tagged code if rollback is needed.

## Development

See [DEVELOPMENT.md](DEVELOPMENT.md) for contributor setup and automated tests.

## References

- [Restic installation](https://restic.readthedocs.io/en/stable/020_installation.html)
- [S3-compatible repositories](https://restic.readthedocs.io/en/stable/030_preparing_a_new_repo.html)
- [Restic retention](https://restic.readthedocs.io/en/stable/060_forget.html)
- [Restic integrity checks](https://restic.readthedocs.io/en/stable/045_working_with_repos.html)
- [MySQL dump behavior](https://dev.mysql.com/doc/refman/8.0/en/mysqldump.html)
