#!/usr/bin/env python3
"""Sequential Forge site backups with validated SQL, restic and Discord."""
import argparse
import fcntl
import json
import logging
import os
import re
import shutil
import signal
import socket
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
import yaml

from database import dump_database
from notifications import job_failure, site_notification, summary_notification
from restic_backend import Restic
from runtime import BackupError, check_space, private_directory

SCRIPT_DIR = Path(__file__).resolve().parent


def positive_int(value, name, allow_zero=False):
    if type(value) is not int or value < (0 if allow_zero else 1):
        raise BackupError(f'{name} must be a {"nonnegative" if allow_zero else "positive"} integer')
    return value


def load_config(path):
    try:
        config = yaml.safe_load(path.read_text())
    except (OSError, yaml.YAMLError):
        raise BackupError('Cannot read configuration YAML') from None
    if not isinstance(config, dict):
        raise BackupError('Configuration must be a mapping')
    global_config = config.get('global', {})
    defaults = config.get('defaults', {})
    sites = config.get('sites', [])
    if not isinstance(global_config, dict) or not isinstance(defaults, dict) or not isinstance(sites, list) or not sites:
        raise BackupError('global/defaults must be mappings and sites must be a nonempty list')
    if 's3' in global_config:
        raise BackupError('Legacy global.s3 configuration: migrate to global.restic (see README)')
    restic = global_config.get('restic', {})
    if not isinstance(restic, dict) or not isinstance(restic.get('repository'), str) or not restic['repository']:
        raise BackupError('global.restic.repository is required')
    restic['config_file'] = str(path)
    for key in ('environment_file', 'password_file'):
        if key in restic and (not isinstance(restic[key], str) or not Path(restic[key]).is_absolute()):
            raise BackupError(f'restic.{key} must be absolute')
    for name, default in [('cpu_cores', 1), ('connections', 2), ('timeout_seconds', 7200),
                          ('check_interval_days', 7), ('check_subsets', 4), ('prune_interval_days', 7)]:
        positive_int(restic.get(name, default), name)
    if not re.fullmatch(r'\d+[KMGTP]?', str(restic.get('max_repack_size', '128M'))):
        raise BackupError('max_repack_size must be a size such as 128M')
    server = global_config.setdefault('server_id', socket.gethostname())
    if not isinstance(server, str) or not re.fullmatch(r'[A-Za-z0-9_.-]+', server):
        raise BackupError('server_id must contain only letters, digits, dots, underscores and hyphens')
    for key in ('success_notification', 'summary_notification'):
        if type(global_config.get(key, True)) is not bool:
            raise BackupError(f'{key} must be true or false')
    global_config['work_dir'] = str(Path(global_config.get('work_dir', path.parent / '.backup-work')).absolute())
    positive_int(global_config.get('min_free_mb', 1024), 'min_free_mb')
    names = set()
    normalized = []
    for raw in sites:
        if not isinstance(raw, dict):
            raise BackupError('Each site must be a mapping')
        site = {**defaults, **raw}
        if 'retention_days' in site or 'compression_level' in site:
            raise BackupError('Replace retention_days/compression_level with retention; see README')
        name = site.get('name')
        if not isinstance(name, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*', name) or name in names:
            raise BackupError('Site names must be unique, safe identifiers')
        names.add(name)
        root = site.get('user_path')
        if not isinstance(root, str) or not Path(root).is_absolute():
            raise BackupError('Each user_path must be absolute')
        site['user_path'] = str(Path(root).resolve())
        if restic['repository'].startswith('/'):
            repository = Path(restic['repository']).resolve()
            if repository == Path(site['user_path']) or Path(site['user_path']) in repository.parents:
                raise BackupError('A local repository must not be inside a backed-up site')
        work = Path(global_config['work_dir']).resolve()
        if work == Path(site['user_path']) or work in Path(site['user_path']).parents:
            raise BackupError('Site paths must not be inside work_dir')
        site.setdefault('backup_database', True)
        if type(site['backup_database']) is not bool:
            raise BackupError('backup_database must be true or false')
        notify = site.get('success_notification')
        if notify is None:
            site['success_notification'] = global_config.get('success_notification', True)
        elif type(notify) is not bool:
            raise BackupError('success_notification must be true or false')
        retention = {'daily': 7, 'weekly': 4, 'monthly': 12}
        for source in (defaults, raw):
            value = source.get('retention', {})
            if not isinstance(value, dict) or set(value) - set(retention):
                raise BackupError('retention supports daily, weekly and monthly')
            retention.update(value)
        for key, value in retention.items():
            positive_int(value, f'retention.{key}', allow_zero=True)
        if not any(retention.values()):
            raise BackupError('At least one retention count must be positive')
        site['retention'] = retention
        patterns = site.get('exclude_patterns', [])
        if not isinstance(patterns, list) or any(not isinstance(p, str) or not p or p.startswith(('/', '!')) or '..' in p.split('/') or '\n' in p for p in patterns):
            raise BackupError('Exclusions must be nonempty relative patterns without negation or parent traversal')
        database = site.get('database', {})
        if not isinstance(database, dict):
            raise BackupError('database must be a mapping')
        positive_int(database.get('timeout_seconds', 1800), 'database.timeout_seconds')
        for key in ('config_file', 'option_file'):
            if key in database and (not isinstance(database[key], str) or not Path(database[key]).is_absolute()):
                raise BackupError(f'database.{key} must be absolute')
        normalized.append(site)
    return global_config, normalized


class BackupScript:
    def __init__(self, config_path):
        self.settings, self.sites = load_config(config_path)
        self.work_dir = private_directory(self.settings['work_dir'])
        self.reserve = self.settings.get('min_free_mb', 1024) * 1024 * 1024
        self.lock = None
        self.restic = None

    def notify(self, payload):
        url = self.settings.get('discord_webhook_url')
        if not url:
            return
        try:
            response = requests.post(url, json=payload, timeout=10)
            response.raise_for_status()
        except requests.RequestException:
            logging.warning('Discord notification could not be delivered')

    def backup_site(self, site):
        name = site['name']
        stage = self.work_dir / 'staging' / name
        snapshot = None
        started = time.monotonic()
        phase = 'Preparing site'
        def report(status, **details):
            self.notify(site_notification(self.settings['server_id'], site, status,
                                          time.monotonic() - started, **details))
        try:
            root = Path(site['user_path'])
            if not root.is_dir():
                raise BackupError('Site directory does not exist')
            with os.scandir(root) as entries:
                next(entries, None)  # Detect an unreadable root before adding other sources.
            # Clear leftovers from interrupted runs before constructing this snapshot.
            if stage.exists():
                if stage.is_symlink():
                    raise BackupError('Staging directory must not be a symlink')
                shutil.rmtree(stage)
            private_directory(stage)
            check_space(self.work_dir, self.reserve)
            logging.info('Backing up site %s', name)
            database = None
            if site['backup_database']:
                phase = 'Database dump and validation'
                database = dump_database(site, stage, self.work_dir / 'secrets', self.reserve,
                                         site.get('database', {}).get('timeout_seconds', 1800))
            manifest = {'format': 1, 'site': name, 'server': self.settings['server_id'],
                        'site_path': str(root), 'database': database,
                        'created_at': datetime.now(timezone.utc).isoformat()}
            (stage / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
            phase = 'Restic snapshot'
            snapshot = self.restic.backup(site, stage, self.settings['server_id'])
            try:
                self.restic.forget(site, self.settings['server_id'])
            except BackupError as error:
                logging.error('Site %s backed up, but retention failed: %s', name, error)
                report('warning', snapshot=snapshot, database=database, error=str(error))
                return True, False
            logging.info('Site %s complete; snapshot %s', name, snapshot)
            if site['success_notification']:
                report('success', snapshot=snapshot, database=database)
            return True, True
        except (BackupError, OSError) as error:
            # OSError may include paths but not credential contents.
            message = str(error) if isinstance(error, BackupError) else 'Local filesystem operation failed'
            logging.error('Site %s failed: %s', name, message)
            report('error', error=message, phase=phase)
            return False, True
        finally:
            if stage.is_dir() and not stage.is_symlink():
                shutil.rmtree(stage)

    def run(self, command):
        started = time.monotonic()
        self.lock = (self.work_dir / 'backup.lock').open('a')
        try:
            try:
                fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise BackupError('Another backup or maintenance job is running') from None
            for name in ('staging', 'secrets', 'tmp', 'cache'):
                private_directory(self.work_dir / name)
            # A killed process can leave credentials; only our private files are removed.
            for stale in (self.work_dir / 'secrets').glob('mysql-*.cnf'):
                stale.unlink()
            self.restic = Restic(self.settings['restic'], self.work_dir, self.reserve)
            self.restic.preflight()
            if command == 'init':
                self.restic.run(['init', '--repository-version', '2'])
                logging.info('Repository initialized')
                return 0
            # Authentication/repository failures must never implicitly initialize a repo.
            self.restic.run(['cat', 'config'])
            if command in ('maintenance', 'check'):
                self.restic.maintenance(full=command == 'check')
                logging.info('Repository maintenance completed')
                return 0
            results = [self.backup_site(site) for site in self.sites]
            successful = sum(backup for backup, _ in results)
            failures = len(results) - successful
            maintenance_failures = sum(not retained for _, retained in results)
            summary = (f'Sites processed: {len(results)}\nSuccessful: {successful}\n'
                       f'Failed: {failures}\nRetention failures: {maintenance_failures}')
            failed = failures or maintenance_failures
            logging.info(summary.replace('\n', '; '))
            if self.settings.get('summary_notification', True):
                self.notify(summary_notification(self.settings['server_id'], self.sites, results,
                                                 time.monotonic() - started))
            return 1 if failed else 0
        except (BackupError, OSError) as error:
            message = str(error) if isinstance(error, BackupError) else 'Local filesystem operation failed'
            logging.error('%s', message)
            self.notify(job_failure(self.settings['server_id'], command, message,
                                    time.monotonic() - started))
            return 1
        finally:
            self.lock.close()  # Keep the inode: unlinking an flock file creates races.


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, default=SCRIPT_DIR / 'config.yaml')
    parser.add_argument('command', nargs='?', choices=['backup', 'init', 'maintenance', 'check'], default='backup')
    args = parser.parse_args(argv)
    os.umask(0o077)
    logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
    try:
        return BackupScript(args.config.resolve()).run(args.command)
    except (BackupError, OSError) as error:
        logging.error('%s', error if isinstance(error, BackupError) else 'Local filesystem operation failed')
        return 1
    except KeyboardInterrupt:
        logging.error('Backup job interrupted')
        return 130


if __name__ == '__main__':
    signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))
    sys.exit(main())
