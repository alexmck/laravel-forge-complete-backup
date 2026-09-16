#!/usr/bin/env python3
"""Interactive first-run configuration for Forge Backups."""
import copy
import fcntl
import getpass
import hashlib
import os
import re
import shlex
import shutil
import socket
import subprocess
import sys
import tempfile
from pathlib import Path
from urllib.parse import urlparse

# Allow execution as scripts/setup.py using the project's virtual environment.
PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))

import yaml

from backup import BackupScript, load_config
from database import credentials
from restic_backend import Restic
from recovery import atomic_private_write, write_recovery_guide
from runtime import BackupError, private_directory, run_command
from site_discovery import discover_sites, inspect_site


def ask(label, default=None):
    suffix = f' [{default}]' if default is not None else ''
    while True:
        value = input(f'{label}{suffix}: ').strip()
        if value:
            return value
        if default is not None:
            return default
        print('A value is required.')


def yes(label, default=False):
    while True:
        value = input(f'{label} [{"Y/n" if default else "y/N"}]: ').strip().lower()
        if not value:
            return default
        if value in ('y', 'yes', 'n', 'no'):
            return value in ('y', 'yes')
        print('Enter yes or no.')


def secret(label):
    while True:
        value = getpass.getpass(f'{label} (hidden): ')
        if value and not any(c in value for c in '\x00\r\n'):
            return value
        print('Enter a nonempty, single-line value.')


def identifier(label, default):
    while True:
        value = ask(label, default)
        if re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*', value):
            return value
        print('Use letters, digits, dots, underscores or hyphens.')


def endpoint(value):
    """Accept an R2 account ID or an HTTPS S3-compatible endpoint."""
    if re.fullmatch(r'[0-9a-fA-F]{32}', value):
        return f'https://{value.lower()}.r2.cloudflarestorage.com'
    parsed = urlparse(value)
    if (parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password
            or parsed.query or parsed.fragment or parsed.path not in ('', '/')):
        raise BackupError('Use an R2 account ID or HTTPS endpoint without a bucket, credentials or query string')
    return value.rstrip('/')


def dotenv_quote(value):
    # Single-quoted shell syntax is also accepted by python-dotenv. Reject the two
    # characters that would require incompatible shell/dotenv escaping conventions.
    if any(c in value for c in "'\\\x00\r\n"):
        raise BackupError('Storage keys cannot contain quotes, backslashes or line breaks')
    return "'" + value + "'"


def choose_database(site, sources):
    binary = shutil.which('mysqldump') or shutil.which('mariadb-dump')
    if not binary:
        raise BackupError('Install a MySQL/MariaDB dump client, then rerun setup; no configuration has been saved')
    site['database'] = {'dump_binary': binary}
    if len(sources) == 1:
        site['database']['config_file'] = sources[0]
    while True:
        try:
            # Multiple distinct config files require an explicit selection even if
            # the older runtime discovery would recognize only one of their paths.
            if len(sources) > 1 and 'config_file' not in site['database'] and 'option_file' not in site['database']:
                raise BackupError('Several database configuration files were found.')
            credentials(site)
            print(f"  {site['name']}: database settings detected; included in backups.")
            break
        except (BackupError, OSError) as error:
            print(str(error) if isinstance(error, BackupError) else 'Cannot read database configuration.')
            for index, source in enumerate(sources, 1):
                print(f'  {index}. {source}')
            value = ask('Database config number/path (.env, PHP or .cnf), or "none" for files only')
            if value.lower() == 'none':
                site['backup_database'] = False
                site.pop('database', None)
                print(f"  {site['name']}: files only, database disabled.")
                return
            if value.isdigit() and 1 <= int(value) <= len(sources):
                value = sources[int(value) - 1]
            source = Path(value).expanduser()
            if not source.is_absolute() or not source.is_file():
                print('Enter a listed number or an existing absolute file path.')
                continue
            site['database'] = {'dump_binary': binary}
            if source.suffix == '.cnf':
                site['database'].update(option_file=str(source), name=ask('Database name'))
            else:
                site['database']['config_file'] = str(source)


def collect_site(names, candidate=None):
    automatic = candidate is not None
    if candidate is None:
        while True:
            path = Path(ask('Site directory (absolute path)')).expanduser()
            if not path.is_absolute() or not path.is_dir():
                print('Enter an existing absolute directory path.')
                continue
            candidate = inspect_site(path, force=True)
            if candidate is None or candidate['issue']:
                print(candidate['issue'] if candidate else 'Cannot inspect that directory.')
                continue
            break
    if candidate['issue']:
        raise BackupError(candidate['issue'])
    path = Path(candidate['path'])
    default_name = re.sub(r'[^A-Za-z0-9_.-]', '-', path.name).strip('.-') or 'site'
    name = default_name
    if automatic:
        suffix = 2
        while name in names:
            name = f'{default_name}-{suffix}'
            suffix += 1
    else:
        while True:
            name = identifier('Site name', default_name)
            if name not in names:
                break
            print('Choose a unique site name.')
    sources = candidate['configs']
    if automatic and sources:
        backup_database = True
    elif automatic and candidate['kind'] == 'Static':
        backup_database = False
        print(f'  {name}: static site, files only.')
    else:
        backup_database = yes(f'Back up the database for {name}?', True)
    site = {'name': name, 'user_path': str(path), 'backup_database': backup_database}
    if candidate['deployment']:
        print(f'  {name}: backing up deployment root {path} (releases and shared files included).')
    if backup_database:
        choose_database(site, sources)
    return site


def selection(value, count):
    if value.lower() == 'all':
        return list(range(count))
    if value.lower() == 'manual':
        return []
    indices = set()
    for token in re.split(r'[,\s]+', value.strip()):
        if not re.fullmatch(r'\d+(?:-\d+)?', token):
            raise BackupError('Choose all, manual, or site numbers such as 1,3 or 1-3')
        ends = list(map(int, token.split('-')))
        first, last = ends[0], ends[-1]
        if not 1 <= first <= last <= count:
            raise BackupError('Site number is outside the displayed list')
        indices.update(range(first - 1, last))
    return sorted(indices)


def collect_sites(project):
    print('\nLooking for sites in /home and enabled Nginx configurations…')
    candidates, warnings = discover_sites(project)
    for warning in warnings:
        print(f'  Notice: {warning}')
    available = []
    for candidate in candidates:
        if candidate['issue']:
            print(f"  Unavailable: {candidate['path']} — {candidate['issue']}")
        else:
            available.append(candidate)
    sites = []
    if available:
        print('\nDetected sites:')
        for index, candidate in enumerate(available, 1):
            database = ('database config found' if candidate['configs'] else
                        'files only' if candidate['kind'] == 'Static' else 'database needs confirmation')
            deployment = ' · zero-downtime layout' if candidate['deployment'] else ''
            print(f"  {index}. {candidate['path']} — {candidate['kind']} · {database}{deployment}")
        while True:
            try:
                chosen = selection(ask('Sites to back up: all, numbers, or manual', 'all'), len(available))
                break
            except BackupError as error:
                print(error)
        for index in chosen:
            sites.append(collect_site({site['name'] for site in sites}, available[index]))
    else:
        print('No readable sites were detected. Enter a site path manually.')
    if not sites or yes('Add a site manually?'):
        while True:
            site = collect_site({site['name'] for site in sites})
            if any(existing['user_path'] == site['user_path'] for existing in sites):
                print('That directory is already selected.')
                continue
            sites.append(site)
            if not yes('Add another site?'):
                break
    return sites


def collect_config(project):
    print('\nCreate the R2 bucket and API credentials in Cloudflare first.')
    print('Save a repository password in your password manager; paste it when prompted.\n')
    default_server = re.sub(r'[^A-Za-z0-9_.-]', '-', socket.gethostname()).strip('.-') or 'forge-server'
    server = identifier('Server name', default_server)
    while True:
        try:
            storage_endpoint = endpoint(ask('R2 account ID or S3 HTTPS endpoint'))
            break
        except BackupError as error:
            print(error)
    while True:
        bucket = ask('Existing bucket name')
        if re.fullmatch(r'[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]', bucket):
            break
        print('Enter a bucket name using lowercase letters, digits, dots or hyphens (3–63 characters).')
    prefix = identifier('Repository prefix', server)
    region = 'auto' if storage_endpoint.endswith('.r2.cloudflarestorage.com') else ask('S3 region', 'us-east-1')
    while True:
        access = secret('S3 access key ID')
        key = secret('S3 secret access key')
        try:
            environment = f'AWS_ACCESS_KEY_ID={dotenv_quote(access)}\nAWS_SECRET_ACCESS_KEY={dotenv_quote(key)}\n'
            break
        except BackupError as error:
            print(error)
    while True:
        password = secret('Repository password from your password manager')
        if password == secret('Confirm repository password'):
            break
        print('Passwords did not match. Try again.')
    webhook = getpass.getpass('Discord webhook URL (hidden; Enter to disable): ').strip()
    if webhook and not webhook.startswith('https://'):
        raise BackupError('Discord webhook must be an HTTPS URL')
    sites = collect_sites(project)
    bundled = project / 'bin/restic'
    binary = str(bundled) if bundled.is_file() else shutil.which('restic')
    if not binary:
        raise BackupError('Restic is missing; run install.sh without --skip-restic or put restic on PATH')
    config = {
        'global': {
            'server_id': server, 'discord_webhook_url': webhook,
            'success_notification': True, 'summary_notification': True,
            'work_dir': str(project / '.backup-work'), 'min_free_mb': 1024,
            'restic': {
                'binary': binary, 'repository': f's3:{storage_endpoint}/{bucket}/{prefix}',
                'region': region, 'environment_file': str(project / 'restic.env'),
                'password_file': str(project / 'restic-password'), 'cpu_cores': 1,
                'connections': 2, 'timeout_seconds': 7200, 'check_interval_days': 7,
                'check_subsets': 4, 'prune_interval_days': 7, 'max_repack_size': '128M',
            },
        },
        'defaults': {'backup_database': True, 'retention': {'daily': 7, 'weekly': 4, 'monthly': 12}},
        'sites': sites,
    }
    # Validate the complete configuration before persisting any secrets.
    with tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', dir=project) as draft:
        yaml.safe_dump(config, draft, sort_keys=False)
        draft.flush()
        load_config(Path(draft.name))
    return config, environment, password


def write_config(project, config, environment, password):
    """Create files exclusively, with rollback of only files created by this call."""
    files = [('restic.env', environment), ('restic-password', password + '\n'),
             ('config.yaml', yaml.safe_dump(config, sort_keys=False))]
    created = []
    try:
        for name, contents in files:
            path = project / name
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            created.append(path)
            with os.fdopen(fd, 'w') as stream:
                stream.write(contents)
    except BaseException:
        for path in reversed(created):
            path.unlink(missing_ok=True)
        raise


def ensure_repository(app):
    # Hold the same local lock used by backup.py through the probe and any init.
    with (app.work_dir / 'backup.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise BackupError('Another backup or maintenance job is running') from None
        for name in ('staging', 'secrets', 'tmp', 'cache'):
            private_directory(app.work_dir / name)
        backend = Restic(app.settings['restic'], app.work_dir, app.reserve)
        backend.preflight()
        status, _ = run_command(backend.base + ['cat', 'config'], env=backend.env,
                                timeout=app.settings['restic'].get('timeout_seconds', 7200),
                                space_path=app.work_dir, reserve_bytes=app.reserve)
        if status == 0:
            print('Existing repository connected successfully.')
        elif status == 10:
            print('Creating the restic repository at the configured location…')
            backend.run(['init', '--repository-version', '2'])
            print('Repository initialized.')
        else:
            raise BackupError(f'Repository connection failed (exit {status}). Check credentials, password and bucket access; setup will not initialize after this error')


def schedule_settings(project):
    path = project / 'config.yaml'
    config = yaml.safe_load(path.read_text()) if path.exists() else {}
    schedule = config.get('setup_schedule', {}) if isinstance(config, dict) else {}
    if not isinstance(schedule, dict):
        raise BackupError('setup_schedule must be a mapping')
    result = {'backup': '03:00', 'maintenance': '05:00', **schedule}
    for key in ('backup', 'maintenance'):
        if not isinstance(result[key], str) or not re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d', result[key]):
            raise BackupError('Schedule times must use HH:MM in server local time')
    return result


def save_changes(project, original, config):
    path = project / 'config.yaml'
    # Validate a draft without serializing load_config's runtime-only additions.
    with tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', dir=project) as draft:
        yaml.safe_dump(config, draft, sort_keys=False)
        draft.flush()
        load_config(Path(draft.name))
    if path.read_text() != original:
        raise BackupError('Configuration changed during setup; rerun to avoid overwriting another edit')
    atomic_private_write(project / 'config.yaml.previous', original)
    atomic_private_write(path, yaml.safe_dump(config, sort_keys=False))
    print('Configuration saved. Previous settings: config.yaml.previous (private).')


def edit_sites(project, config):
    existing = config['sites']
    print('Current sites (retained sites keep all custom settings):')
    for index, site in enumerate(existing, 1):
        print(f"  {index}. {site['name']} — {site['user_path']}")
    while True:
        try:
            value = ask('Sites to keep: all, numbers, or none', 'all')
            keep = [] if value.lower() == 'none' else selection(value, len(existing))
            break
        except BackupError as error:
            print(error)
    selected = [copy.deepcopy(existing[index]) for index in keep]
    if not selected or yes('Discover and add more sites?', True):
        candidates, warnings = discover_sites(project)
        for warning in warnings:
            print(f'  Notice: {warning}')
        known = {str(Path(site['user_path']).resolve()) for site in existing}
        available = []
        for candidate in candidates:
            if candidate['path'] in known:
                continue
            if candidate['issue']:
                print(f"  Unavailable: {candidate['path']} — {candidate['issue']}")
            else:
                available.append(candidate)
        if available:
            for index, candidate in enumerate(available, 1):
                print(f"  {index}. {candidate['path']} — {candidate['kind']}")
            while True:
                try:
                    value = ask('New sites to add: all, numbers, or none', 'all')
                    chosen = [] if value.lower() == 'none' else selection(value, len(available))
                    break
                except BackupError as error:
                    print(error)
            for index in chosen:
                selected.append(collect_site({site['name'] for site in existing + selected}, available[index]))
        else:
            print('No new readable sites detected.')
        while yes('Add a site manually?'):
            site = collect_site({site['name'] for site in existing + selected})
            if any(Path(old['user_path']).resolve() == Path(site['user_path']).resolve() for old in selected):
                print('That directory is already selected.')
            else:
                selected.append(site)
    if not selected:
        raise BackupError('Keep at least one site; no changes have been saved')
    config['sites'] = selected
    print('Selected: ' + ', '.join(site['name'] for site in selected))
    print('Removing a site stops future backups; its existing snapshots are not deleted.')


def edit_retention(config):
    defaults = config.setdefault('defaults', {})
    retention = {'daily': 7, 'weekly': 4, 'monthly': 12, **defaults.get('retention', {})}
    while True:
        for tier in ('daily', 'weekly', 'monthly'):
            while True:
                value = ask(f'{tier.capitalize()} snapshots to keep', str(retention[tier]))
                if value.isdigit():
                    retention[tier] = int(value)
                    break
                print('Enter a nonnegative integer.')
        if any(retention.values()):
            break
        print('At least one count must be greater than zero.')
    defaults['retention'] = retention
    overrides = [site['name'] for site in config['sites'] if 'retention' in site]
    if overrides:
        print('Sites with retention overrides: ' + ', '.join(overrides))
        if yes('Apply these defaults to those sites too (remove overrides)?'):
            for site in config['sites']:
                site.pop('retention', None)
    print('Retention takes effect after future successful backups and may remove older snapshots.')


def manage_existing(project):
    while True:
        print('\nExisting setup:')
        print('  1. Continue: connect, optional backup and scheduling')
        print('  2. Manage sites (discover, add or remove)')
        print('  3. Change daily/weekly/monthly retention')
        print('  4. Change backup and maintenance schedule')
        print('  5. Regenerate recovery guide')
        print('  0. Exit')
        choice = ask('Choose an option', '1')
        if choice == '0':
            return False
        if choice == '1':
            return True
        if choice == '5':
            settings, sites = load_config(project / 'config.yaml')
            print(f'Recovery guide saved: {write_recovery_guide(project, settings, sites)}')
            continue
        if choice not in ('2', '3', '4'):
            print('Choose a displayed option.')
            continue
        path = project / 'config.yaml'
        original = path.read_text()
        load_config(path)
        config = yaml.safe_load(original)
        if choice == '2':
            edit_sites(project, config)
        elif choice == '3':
            edit_retention(config)
        else:
            schedule = schedule_settings(project)
            for job in ('backup', 'maintenance'):
                while True:
                    value = ask(f'Daily {job} time (HH:MM, server local time)', schedule[job])
                    if re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d', value):
                        schedule[job] = value
                        break
                    print('Use a time from 00:00 to 23:59.')
            config['setup_schedule'] = schedule
            print(f"Proposed schedule: backup {schedule['backup']}, maintenance {schedule['maintenance']}.")
        if not yes('Save these changes?', True):
            print('Changes discarded.')
            continue
        save_changes(project, original, config)
        settings, sites = load_config(path)
        print(f'Recovery guide saved: {write_recovery_guide(project, settings, sites)}')
        if choice == '4':
            for line in cron_lines(project):
                print(line)
            if yes('Install/update these jobs in this user\'s crontab? Choose no for Forge scheduling'):
                install_schedule(project)
            else:
                print('Saved preferred times only. Existing scheduled jobs are unchanged; update Forge with the printed commands.')


def cron_lines(project):
    # cron interprets percent signs even inside shell quotes.
    def quote(path):
        return shlex.quote(str(path)).replace('%', r'\%')
    command = f'{quote(project / "venv/bin/python")} {quote(project / "backup.py")}'
    log = quote(project / 'cron.log')
    schedule = schedule_settings(project)
    lines = []
    for job in ('backup', 'maintenance'):
        hour, minute = map(int, schedule[job].split(':'))
        lines.append(f'{minute} {hour} * * * /usr/bin/nice -n 10 {command} {job} >> {log} 2>&1')
    return lines


def update_crontab(existing, project):
    identity = hashlib.sha256(str(project).encode()).hexdigest()[:12]
    begin = f'# BEGIN forge-backups {identity}'
    end = f'# END forge-backups {identity}'
    if begin in existing or end in existing:
        if existing.count(begin) != 1 or existing.count(end) != 1:
            raise BackupError('Ambiguous existing backup cron block; edit crontab manually')
        before, _, rest = existing.partition(begin)
        _, found, after = rest.partition(end)
        if not found:
            raise BackupError('Invalid existing backup cron block')
        existing = before + after.lstrip('\n')
    if any('backup.py' in line and not line.lstrip().startswith('#') for line in existing.splitlines()):
        raise BackupError('An existing backup.py cron job was found; update that job instead of adding duplicates')
    prefix = existing.rstrip('\n')
    block = '\n'.join([begin, *cron_lines(project), end]) + '\n'
    return prefix + '\n\n' + block if prefix else block


def install_schedule(project):
    if not shutil.which('crontab'):
        raise BackupError('crontab is unavailable; add the printed commands through Forge')
    env = {**os.environ, 'LC_ALL': 'C'}
    result = subprocess.run(['crontab', '-l'], capture_output=True, text=True, env=env, timeout=30)
    if result.returncode and not (result.returncode == 1 and 'no crontab for' in result.stderr):
        raise BackupError('Cannot read existing crontab; no schedule was changed')
    updated = update_crontab(result.stdout if result.returncode == 0 else '', project)
    result = subprocess.run(['crontab', '-'], input=updated, capture_output=True, text=True, env=env, timeout=30)
    if result.returncode:
        raise BackupError('Could not install the schedule')
    schedule = schedule_settings(project)
    print(f"Scheduled: backups at {schedule['backup']}; maintenance at {schedule['maintenance']} (server local time).")


def setup(project):
    config_path = project / 'config.yaml'
    if config_path.exists():
        print('Using existing config.yaml. Storage credentials and passwords are preserved.')
        if not manage_existing(project):
            return
    else:
        if any(os.path.lexists(project / name) for name in ('config.yaml', 'restic.env', 'restic-password')):
            raise BackupError('Existing configuration/secret files found without a usable config.yaml. Recover the configuration or move those files aside before setup; they will not be overwritten')
        config, environment, password = collect_config(project)
        print(f"\nRepository: {config['global']['restic']['repository']}")
        print('Sites: ' + ', '.join(site['name'] for site in config['sites']))
        print('Retention: 7 daily, 4 weekly, 12 monthly. Settings can be edited in config.yaml.')
        write_config(project, config, environment, password)
        print('Saved config.yaml, restic.env and restic-password with private permissions.')
    app = BackupScript(config_path)
    ensure_repository(app)
    print(f'Recovery guide saved: {write_recovery_guide(project, app.settings, app.sites)}')
    print('Keep a copy off this server alongside the password in your password manager.')
    if yes('Run the first backup now? (sends configured Discord notifications)', True):
        if app.run('backup'):
            raise BackupError('Backup failed. Settings are saved; correct the reported issue and rerun install.sh')
        print('Backup completed successfully.')
    print('\nDaily schedule (server local time):')
    for line in cron_lines(project):
        print(line)
    if yes('Install these jobs in this user\'s crontab? Choose no if Forge already schedules backups'):
        install_schedule(project)
    print('\nSetup complete. Recovery instructions are in README.md.')


def main():
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        print('Guided setup requires an interactive terminal. Use install.sh --skip-setup for unattended dependency installation.', file=sys.stderr)
        return 1
    os.umask(0o077)
    try:
        setup(PROJECT)
        return 0
    except (BackupError, OSError, subprocess.SubprocessError) as error:
        print(str(error) if isinstance(error, BackupError) else 'Setup failed during a local operation; check permissions and available tools.', file=sys.stderr)
        return 1
    except (KeyboardInterrupt, EOFError):
        print('\nSetup cancelled.', file=sys.stderr)
        return 130


if __name__ == '__main__':
    sys.exit(main())
