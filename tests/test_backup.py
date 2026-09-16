import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import yaml

from backup import BackupScript, load_config
from database import credentials, discover, dump_database, option_value, php_literal
from restic_backend import Restic
from runtime import BackupError, private_directory, run_command


class Fixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.site = self.root / 'site'
        self.site.mkdir()
        self.config_path = self.root / 'config.yaml'
        self.config = {
            'global': {'server_id': 'test', 'work_dir': str(self.root / 'work'),
                       'min_free_mb': 1, 'restic': {'repository': str(self.root / 'repo')}},
            'sites': [{'name': 'app', 'user_path': str(self.site), 'backup_database': False}],
        }
        self.save()

    def save(self):
        self.config_path.write_text(yaml.safe_dump(self.config))

    def app(self):
        self.save()
        app = BackupScript(self.config_path)
        for directory in ('staging', 'secrets', 'tmp', 'cache'):
            private_directory(app.work_dir / directory)
        app.restic = Mock()
        app.restic.backup.return_value = 'a' * 64
        app.notify = Mock()
        return app


class ConfigTests(Fixture):
    def test_retention_inheritance(self):
        self.config['defaults'] = {'retention': {'daily': 3, 'weekly': 2}}
        self.config['sites'][0]['retention'] = {'daily': 5}
        self.save()
        _, sites = load_config(self.config_path)
        self.assertEqual(sites[0]['retention'], {'daily': 5, 'weekly': 2, 'monthly': 12})

    def test_invalid_configs(self):
        for update in ({'retention': {'daily': -1}}, {'backup_database': 'false'},
                       {'retention_days': 3}, {'name': '../escape'},
                       {'exclude_patterns': ['../secrets']}, {'database': None}):
            with self.subTest(update=update):
                self.config['sites'][0] = {'name': 'app', 'user_path': str(self.site), **update}
                self.save()
                with self.assertRaises(BackupError):
                    load_config(self.config_path)

    def test_legacy_s3_rejected(self):
        self.config['global']['s3'] = {}
        self.save()
        with self.assertRaisesRegex(BackupError, 'Legacy'):
            load_config(self.config_path)

    def test_site_under_work_rejected(self):
        self.config['sites'][0]['user_path'] = str(self.root / 'work/staging/app')
        self.save()
        with self.assertRaises(BackupError):
            load_config(self.config_path)


class DatabaseTests(Fixture):
    def test_dotenv_quoting_and_empty_password(self):
        (self.site / '.env').write_text('DB_DATABASE=app\nDB_USERNAME=forge\nDB_PASSWORD="a # b" # comment\n')
        self.assertEqual(discover(self.site)['password'], 'a # b')
        (self.site / '.env').write_text("DB_DATABASE=app\nDB_USERNAME=forge\nDB_PASSWORD=''\n")
        self.assertEqual(credentials({'user_path': str(self.site)})['password'], '')

    def test_ambiguous_source_fails(self):
        (self.site / '.env').touch()
        (self.site / 'public').mkdir()
        (self.site / 'public/wp-config.php').touch()
        with self.assertRaisesRegex(BackupError, 'exactly one'):
            discover(self.site)

    def test_localhost_nonstandard_port_uses_tcp(self):
        (self.site / '.env').write_text('DB_DATABASE=app\nDB_USERNAME=forge\nDB_PASSWORD=pass\nDB_HOST=localhost\nDB_PORT=3307\n')
        config = credentials({'user_path': str(self.site)})
        self.assertEqual(config['host'], '127.0.0.1')
        self.assertEqual(config['port'], '3307')

    def test_explicit_socket_is_preserved(self):
        config = credentials({'database': {'name': 'app', 'user': 'forge', 'password': '',
                                          'host': 'localhost', 'port': 3307, 'socket': '/var/run/mysql.sock'}})
        self.assertEqual(config['host'], 'localhost')
        self.assertEqual(config['socket'], '/var/run/mysql.sock')

    def test_php_literals(self):
        self.assertEqual(php_literal(r"'a\'b\\c'"), "a'b\\c")
        self.assertEqual(php_literal(r'"a\"b\$c"'), 'a"b$c')
        with self.assertRaises(BackupError):
            php_literal('"$secret"')

    def test_php_applications(self):
        public = self.site / 'public'
        public.mkdir()
        configs = {
            'wp-config.php': "<?php\n// define('DB_NAME','wrong');\ndefine('DB_NAME','app'); define('DB_USER','user'); define('DB_PASSWORD','p\\\'ass'); define('DB_HOST','localhost:3307');",
            'LocalSettings.php': "<?php $wgDBname='app'; $wgDBuser='user'; $wgDBpassword='p\\\'ass'; $wgDBserver='localhost:3307';",
            'conf_global.php': "<?php $INFO = array('sql_database'=>'app', 'sql_user'=>'user', 'sql_pass'=>'p\\\'ass', 'sql_host'=>'localhost:3307');",
        }
        for filename, content in configs.items():
            with self.subTest(filename=filename):
                path = public / filename
                path.write_text(content)
                config = credentials({'user_path': str(self.site)})
                self.assertEqual(config['name'], 'app')
                self.assertEqual(config['password'], "p'ass")
                self.assertEqual(config['port'], '3307')
                path.unlink()

    def test_dynamic_php_fails(self):
        path = self.site / 'wp-config.php'
        path.write_text("<?php define('DB_NAME', 'app'); define('DB_USER','user'); define('DB_PASSWORD', getenv('SECRET')); define('DB_HOST','localhost');")
        with self.assertRaises(BackupError):
            discover(self.site, path)

    def test_option_escape(self):
        self.assertEqual(option_value('a"b\\c\nd'), '"a\\"b\\\\c\\nd"')

    def fake_dump(self, body, status=0):
        app = self.app()
        site = app.sites[0]
        site['database'] = {'name': 'app', 'user': 'user', 'password': 'secret"\\value'}
        stage = private_directory(app.work_dir / 'staging/app')
        def execute(args, **kwargs):
            if '--version' in args:
                return 0, b'mysqldump Ver 8.0.36'
            self.assertNotIn(site['database']['password'], ' '.join(args))
            option = Path(args[1].split('=', 1)[1])
            self.assertEqual(option.stat().st_mode & 0o777, 0o600)
            self.assertIn('password=', option.read_text())
            kwargs['output'](body)
            return status, b''
        return app, site, stage, execute

    def test_dump_success_and_cleanup(self):
        app, site, stage, execute = self.fake_dump(b'-- SQL\n-- Dump completed on 2026-09-16 00:00:00\n')
        with patch('database.shutil.which', return_value='/usr/bin/mysqldump'), patch('database.run_command', side_effect=execute):
            result = dump_database(site, stage, app.work_dir / 'secrets', 0)
        self.assertTrue((stage / 'database.sql').exists())
        self.assertEqual(len(result['sha256']), 64)
        self.assertEqual(list((app.work_dir / 'secrets').iterdir()), [])

    def test_failed_empty_and_incomplete_dumps(self):
        for body, status in [(b'', 0), (b'SELECT 1;', 0), (b'-- Dump completed on today\n', 2)]:
            with self.subTest(body=body, status=status):
                app, site, stage, execute = self.fake_dump(body, status)
                with patch('database.shutil.which', return_value='mysqldump'), patch('database.run_command', side_effect=execute):
                    with self.assertRaises(BackupError):
                        dump_database(site, stage, app.work_dir / 'secrets', 0)
                self.assertFalse((stage / 'database.sql').exists())
                self.assertFalse((stage / 'database.sql.partial').exists())
                self.assertEqual(list((app.work_dir / 'secrets').iterdir()), [])

    def test_low_disk_aborts_dump(self):
        app, site, stage, execute = self.fake_dump(b'-- data')
        with patch('database.shutil.which', return_value='mysqldump'), patch('database.run_command', side_effect=execute), patch('database.check_space', side_effect=BackupError('reserve')):
            with self.assertRaises(BackupError):
                dump_database(site, stage, app.work_dir / 'secrets', 0)
        self.assertEqual(list((app.work_dir / 'secrets').iterdir()), [])


class OrchestrationTests(Fixture):
    def test_database_failure_prevents_snapshot_and_retention(self):
        app = self.app()
        app.sites[0]['backup_database'] = True
        with patch('backup.dump_database', side_effect=BackupError('dump failed')):
            self.assertEqual(app.backup_site(app.sites[0]), (False, True))
        app.restic.backup.assert_not_called()
        app.restic.forget.assert_not_called()
        self.assertIn('failed', app.notify.call_args.args[0]['embeds'][0]['title'])

    def test_restic_failure_prevents_retention(self):
        app = self.app()
        app.restic.backup.side_effect = BackupError('partial snapshot')
        self.assertEqual(app.backup_site(app.sites[0]), (False, True))
        app.restic.forget.assert_not_called()

    def test_retention_failure_keeps_backup_success_separate(self):
        app = self.app()
        app.restic.forget.side_effect = BackupError('retention failed')
        self.assertEqual(app.backup_site(app.sites[0]), (True, False))

    def test_success_notification_override(self):
        app = self.app()
        app.sites[0]['success_notification'] = False
        self.assertEqual(app.backup_site(app.sites[0]), (True, True))
        app.notify.assert_not_called()

    def test_failed_site_returns_nonzero_and_continues(self):
        app = self.app()
        app.sites.append({**app.sites[0], 'name': 'second'})
        with patch('backup.Restic'), patch.object(app, 'backup_site', side_effect=[(False, True), (True, True)]) as backup:
            self.assertEqual(app.run('backup'), 1)
            self.assertEqual(backup.call_count, 2)

    def test_lock_contention(self):
        import fcntl
        app = self.app()
        with (app.work_dir / 'backup.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with patch('backup.Restic') as restic:
                self.assertEqual(app.run('backup'), 1)
                restic.assert_not_called()


class ResticTests(Fixture):
    def backend(self):
        work = private_directory(self.root / 'work')
        with patch.dict(os.environ, {'RESTIC_PASSWORD': 'test'}):
            return Restic(self.config['global']['restic'], work, 0)

    def test_partial_snapshot_is_not_tagged(self):
        backend = self.backend()
        site = self.app().sites[0]
        with patch('restic_backend.run_command', return_value=(3, b'')) as run:
            with self.assertRaisesRegex(BackupError, 'incomplete'):
                backend.backup(site, self.root, 'test')
        self.assertEqual(run.call_count, 1)

    def test_retention_filters(self):
        backend = self.backend()
        backend.run = Mock()
        backend.forget(self.app().sites[0], 'test')
        args = backend.run.call_args.args[0]
        self.assertIn('forge-backup,server:test,site:app,complete', args)
        self.assertIn('host,tags', args)
        self.assertNotIn('--prune', args)

    def test_check_failure_does_not_advance_state_or_prune(self):
        backend = self.backend()
        backend.run = Mock(side_effect=BackupError('check failed'))
        with self.assertRaises(BackupError):
            backend.maintenance()
        self.assertEqual(backend.run.call_count, 1)
        self.assertEqual(list(backend.work_dir.glob('maintenance-*.json')), [])

    def test_check_rotation_and_due_state(self):
        backend = self.backend()
        backend.run = Mock()
        backend.maintenance()
        self.assertEqual(backend.run.call_args_list[0].args[0], ['check', '--read-data-subset=1/4'])
        self.assertEqual(backend.run.call_args_list[1].args[0][0], 'prune')
        backend.run.reset_mock()
        backend.maintenance()
        backend.run.assert_not_called()
        path = next(backend.work_dir.glob('maintenance-*.json'))
        state = json.loads(path.read_text())
        state['checked_at'] = 0
        path.write_text(json.dumps(state))
        backend.maintenance()
        backend.run.assert_called_once_with(['check', '--read-data-subset=2/4'])

    def test_full_check(self):
        backend = self.backend()
        backend.run = Mock()
        backend.maintenance(full=True)
        backend.run.assert_called_once_with(['check', '--read-data'])


class ProcessTests(unittest.TestCase):
    def test_timeout(self):
        with self.assertRaisesRegex(BackupError, 'timeout'):
            run_command([sys.executable, '-c', 'import time; time.sleep(10)'], timeout=0.1)

    def test_output_is_bounded(self):
        code, data = run_command([sys.executable, '-c', 'print("x"*500000)'])
        self.assertEqual(code, 0)
        self.assertLessEqual(len(data), 262144)


@unittest.skipUnless(os.environ.get('RESTIC_TEST_BINARY'), 'Set RESTIC_TEST_BINARY for local integration')
class IntegrationTests(Fixture):
    def test_guided_setup_connects_initializes_and_runs_backup(self):
        from scripts import setup
        self.config['global']['restic']['binary'] = os.environ['RESTIC_TEST_BINARY']
        self.save()
        (self.site / 'index.html').write_text('hello from setup')
        with patch.dict(os.environ, {'RESTIC_PASSWORD': 'integration-test-only'}), patch.object(setup, 'manage_existing', return_value=True), patch.object(setup, 'yes', side_effect=[True, False, False, False]):
            setup.setup(self.root)  # Existing YAML, missing repository: initialize and back up.
            setup.setup(self.root)  # Existing repository: reuse it, skip backup and cron.
            app = BackupScript(self.config_path)
            self.assertEqual(app.run('check'), 0)
            snapshots = json.loads(app.restic.run(['snapshots', '--json', '--tag', 'complete']))
            guide = (self.root / 'RECOVERY.md').read_text()
            commands = '\n'.join(re.findall(r'```bash\n(.*?)```', guide, re.S))
            recovery_home = self.root / 'recovery-home'
            recovery_home.mkdir()
            commands = commands.replace('$HOME/forge-restore', '$RECOVERY_TEST_HOME/forge-restore')
            tools_dir = self.root / 'recovery-bin'
            tools_dir.mkdir()
            (tools_dir / 'restic').symlink_to(os.environ['RESTIC_TEST_BINARY'])
            env = {**os.environ, 'RECOVERY_TEST_HOME': str(recovery_home),
                   'RESTIC_CACHE_DIR': str(recovery_home / 'cache'),
                   'PATH': str(tools_dir) + os.pathsep + os.environ['PATH']}
            result = subprocess.run(['bash', '-e', '-c', commands],
                                    input='integration-test-only\n' + snapshots[0]['id'] + '\n',
                                    env=env, text=True, capture_output=True, timeout=60)
            self.assertEqual(result.returncode, 0, result.stderr)
            destination = next(recovery_home.glob('forge-restore.*'))
            restored = destination / str(self.site).lstrip('/') / 'index.html'
            self.assertEqual(restored.read_text(), 'hello from setup')

    def test_backup_restore_exclusions_and_database(self):
        binary = os.environ['RESTIC_TEST_BINARY']
        self.config['global']['restic']['binary'] = binary
        # Also exercise work directory nested within the backed-up site.
        self.config['global']['work_dir'] = str(self.site / '.backup-work')
        self.config['sites'][0]['backup_database'] = True
        self.config['sites'][0]['retention'] = {'daily': 1, 'weekly': 0, 'monthly': 0}
        self.config['sites'][0]['exclude_patterns'] = ['*.sql', '*.log']
        (self.site / 'index.php').write_text('<?php echo "hello";')
        (self.site / 'debug.log').write_text('excluded')
        (self.site / 'nested').mkdir()
        (self.site / 'nested/debug.log').write_text('excluded')
        self.save()
        with patch.dict(os.environ, {'RESTIC_PASSWORD': 'integration-test-only'}):
            app = BackupScript(self.config_path)
            self.assertEqual(app.run('init'), 0)
            def dump(site, stage, *args):
                (stage / 'database.sql').write_text('-- SQL\n-- Dump completed on today\n')
                return {'name': 'app', 'file': str(stage / 'database.sql')}
            with patch('backup.dump_database', side_effect=dump):
                self.assertEqual(app.run('backup'), 0)
                self.assertEqual(app.run('backup'), 0)
            data = app.restic.run(['snapshots', '--json', '--tag', 'complete'])
            snapshots = json.loads(data)
            self.assertEqual(len(snapshots), 1)  # Same-day retention.
            destination = self.root / 'restore'
            app.restic.run(['restore', snapshots[0]['id'], '--target', str(destination)])
            restored_site = destination / str(self.site).lstrip('/')
            self.assertEqual((restored_site / 'index.php').read_text(), '<?php echo "hello";')
            self.assertFalse((restored_site / 'debug.log').exists())
            self.assertFalse((restored_site / 'nested/debug.log').exists())
            stage = restored_site / '.backup-work/staging/app'
            self.assertTrue((stage / 'database.sql').is_file())
            self.assertTrue((stage / 'manifest.json').is_file())
            self.assertFalse((restored_site / '.backup-work/secrets').exists())
            self.assertEqual(app.run('maintenance'), 0)
            self.assertEqual(app.run('check'), 0)


if __name__ == '__main__':
    unittest.main()
