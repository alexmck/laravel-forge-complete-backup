"""Restic snapshots, retention and bounded repository maintenance."""
import hashlib
import json
import math
import os
import re
from datetime import datetime, timezone
from pathlib import Path

from database import read_env
from runtime import BackupError, protected_file, run_command


class Restic:
    def __init__(self, config, work_dir, reserve_bytes):
        self.config = config
        self.work_dir = work_dir
        self.reserve_bytes = reserve_bytes
        self.env = os.environ.copy()
        if config.get('environment_file'):
            values = read_env(protected_file(config['environment_file']))
            allowed = {'AWS_ACCESS_KEY_ID', 'AWS_SECRET_ACCESS_KEY', 'AWS_SESSION_TOKEN',
                       'AWS_DEFAULT_REGION', 'RESTIC_PASSWORD_FILE', 'RESTIC_PASSWORD'}
            if set(values) - allowed or any(v is None for v in values.values()):
                raise BackupError("Invalid key/value in restic environment file")
            self.env.update(values)
        self.env['RESTIC_REPOSITORY'] = config['repository']
        if config.get('password_file'):
            self.env['RESTIC_PASSWORD_FILE'] = config['password_file']
        password_file = self.env.get('RESTIC_PASSWORD_FILE')
        if password_file:
            protected_file(password_file)
            self.env.pop('RESTIC_PASSWORD', None)
        elif not self.env.get('RESTIC_PASSWORD'):
            raise BackupError("Set RESTIC_PASSWORD_FILE or RESTIC_PASSWORD")
        if config['repository'].startswith('s3:'):
            if not all(self.env.get(k) for k in ('AWS_ACCESS_KEY_ID', 'AWS_SECRET_ACCESS_KEY')):
                raise BackupError("S3 access key and secret key are required")
            self.env['AWS_DEFAULT_REGION'] = config.get('region', 'auto')
        self.env['GOMAXPROCS'] = str(config.get('cpu_cores', 1))
        self.env['TMPDIR'] = str(work_dir / 'tmp')
        # Never inherit behavioral settings from an interactive shell.
        for key in ('RESTIC_REPOSITORY_FILE', 'RESTIC_PASSWORD_COMMAND', 'RESTIC_HOST',
                    'RESTIC_PACK_SIZE', 'RESTIC_COMPRESSION', 'RESTIC_READ_CONCURRENCY'):
            self.env.pop(key, None)
        self.base = [config.get('binary', 'restic'), '--repo', config['repository'],
                     '--cache-dir', str(work_dir / 'cache'), '--retry-lock', '30s']
        if config['repository'].startswith('s3:'):
            self.base += ['-o', f"s3.connections={config.get('connections', 2)}"]

    def run(self, args, output=None):
        status, data = run_command(self.base + args, env=self.env,
                                   timeout=self.config.get('timeout_seconds', 7200),
                                   output=output, space_path=self.work_dir,
                                   reserve_bytes=self.reserve_bytes)
        if status:
            detail = ' (incomplete snapshot)' if args[0] == 'backup' and status == 3 else ''
            raise BackupError(f"restic {args[0]} failed with exit {status}{detail}; check repository access, locks, permissions and free memory")
        return data

    def preflight(self):
        status, output = run_command([self.base[0], 'version'], timeout=30)
        version = re.search(rb'restic (\d+)\.(\d+)\.(\d+)', output)
        if status or not version or tuple(map(int, version.groups())) < (0, 19, 1):
            raise BackupError("restic 0.19.1 or newer is required")

    @staticmethod
    def tags(server, site, complete=False):
        tags = ['forge-backup', f'server:{server}', f'site:{site}']
        return tags + ['complete'] if complete else tags

    def backup(self, site, stage, server):
        root = Path(site['user_path'])
        args = ['backup', '--json', '--host', server, '--group-by', 'host,paths',
                '--read-concurrency', '1', '--compression', 'auto']
        for tag in self.tags(server, site['name']):
            args += ['--tag', tag]
        # Always exclude our own work directory, even when located below a site root.
        def literal(path):
            return re.sub(r'([\\*?\[])', r'\\\1', str(path))
        args += ['--exclude', literal(self.work_dir)]
        for secret in (self.config.get('environment_file'), self.env.get('RESTIC_PASSWORD_FILE'),
                       site.get('database', {}).get('option_file'), self.config.get('config_file')):
            if secret:
                args += ['--exclude', literal(Path(secret).resolve())]
        for pattern in site.get('exclude_patterns', []):
            relative = pattern if '/' in pattern else '**/' + pattern
            args += ['--exclude', literal(root) + '/' + relative]
        # Explicit metadata files bypass the work-directory exclusion.
        args += ['--', str(root), str(stage / 'manifest.json')]
        if site['backup_database']:
            args.append(str(stage / 'database.sql'))
        summary = {}
        pending = bytearray()
        def consume(chunk):
            pending.extend(chunk)
            while b'\n' in pending:
                line, _, rest = pending.partition(b'\n')
                pending[:] = rest
                try:
                    message = json.loads(line)
                except (ValueError, UnicodeDecodeError):
                    continue
                if message.get('message_type') == 'summary':
                    summary.update(message)
            if len(pending) > 262144:
                raise BackupError("Unexpectedly large restic output record")
        self.run(args, output=consume)
        snapshot = summary.get('snapshot_id', '')
        if not re.fullmatch(r'[0-9a-f]{8,64}', snapshot):
            raise BackupError("restic did not return a successful snapshot ID")
        # Tagging changes the snapshot ID. Query the original ID returned by tag.
        self.run(['tag', '--add', 'complete', snapshot])
        data = self.run(['snapshots', '--json', '--tag', ','.join(self.tags(server, site['name'], True)), '--group-by', '', '--latest', '1'])
        try:
            snapshots = json.loads(data)
            completed = snapshots[0]['id']
            if snapshots[0].get('original') != snapshot:
                raise ValueError('snapshot mismatch')
        except (ValueError, KeyError, IndexError, TypeError):
            raise BackupError("Could not verify completed snapshot identity") from None
        # Missing statistics are unknown, not zero. Keep the compressed amount
        # distinct from the uncompressed data_added value.
        stats = {key: value for key in ('total_bytes_processed', 'data_added_packed')
                 if type(value := summary.get(key)) is int and value >= 0}
        return {'snapshot': completed, 'stats': stats}

    def forget(self, site, server):
        retention = site['retention']
        self.run(['forget', '--host', server,
                  '--tag', ','.join(self.tags(server, site['name'], True)),
                  '--group-by', 'host,tags',
                  '--keep-daily', str(retention['daily']),
                  '--keep-weekly', str(retention['weekly']),
                  '--keep-monthly', str(retention['monthly'])])

    def maintenance(self, full=False):
        identity = hashlib.sha256(self.config['repository'].encode()).hexdigest()[:16]
        state_path = self.work_dir / f'maintenance-{identity}.json'
        state = {}
        if state_path.exists():
            try:
                state = json.loads(state_path.read_text())
                if not isinstance(state, dict):
                    raise ValueError()
                for key in ('checked_at', 'pruned_at'):
                    value = state.get(key, 0)
                    if type(value) not in (float, int) or not math.isfinite(value) or value < 0:
                        raise ValueError()
                if type(state.get('next_subset', 1)) is not int:
                    raise ValueError()
            except (ValueError, OSError):
                raise BackupError("Invalid maintenance state; inspect it before retrying") from None
        now = datetime.now(timezone.utc).timestamp()
        interval = self.config.get('check_interval_days', 7) * 86400
        parts = self.config.get('check_subsets', 4)
        due = now - float(state.get('checked_at', 0)) >= interval
        if full or due:
            part = int(state.get('next_subset', 1))
            if not 1 <= part <= parts:
                part = 1
            self.run(['check', '--read-data' if full else f'--read-data-subset={part}/{parts}'])
            state.update(checked_at=now, next_subset=1 if full else part % parts + 1)
            self.save_state(state_path, state)
        if not full and now - float(state.get('pruned_at', 0)) >= self.config.get('prune_interval_days', 7) * 86400:
            self.run(['prune', '--max-repack-size', self.config.get('max_repack_size', '128M')])
            state['pruned_at'] = now
            self.save_state(state_path, state)

    @staticmethod
    def save_state(path, state):
        temp = path.with_suffix('.tmp')
        temp.write_text(json.dumps(state) + '\n')
        temp.chmod(0o600)
        temp.replace(path)
