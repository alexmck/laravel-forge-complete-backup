import contextlib
import io
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from database import read_env
from runtime import BackupError
from scripts import setup


class SetupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.project = Path(self.temp.name).resolve()
        self.site = self.project / 'example-site'
        self.site.mkdir()
        (self.project / 'bin').mkdir()
        (self.project / 'bin/restic').touch()
        management = patch.object(setup, 'manage_existing', return_value=True)
        management.start()
        self.addCleanup(management.stop)
        guide = patch.object(setup, 'write_recovery_guide', return_value=self.project / 'RECOVERY.md')
        guide.start()
        self.addCleanup(guide.stop)
        self.output = io.StringIO()
        self.redirect = contextlib.redirect_stdout(self.output)
        self.redirect.__enter__()
        self.addCleanup(self.redirect.__exit__, None, None, None)

    def test_account_id_and_generic_endpoint(self):
        self.assertEqual(setup.endpoint('a' * 32), 'https://' + 'a' * 32 + '.r2.cloudflarestorage.com')
        self.assertEqual(setup.endpoint('https://s3.example.com/'), 'https://s3.example.com')
        for value in ('http://example.com', 'https://user:pass@example.com', 'https://example.com/bucket', 'https://example.com/?key=secret'):
            with self.subTest(value=value), self.assertRaises(BackupError):
                setup.endpoint(value)

    def collect(self):
        answers = ['', 'a' * 32, 'backup-bucket', '', str(self.site), '', 'n', 'n']
        passwords = ['ACCESS', 'SECRET', 'password-from-manager', 'password-from-manager', '']
        with patch('builtins.input', side_effect=answers), patch('getpass.getpass', side_effect=passwords), patch('socket.gethostname', return_value='forge-test'), patch.object(setup, 'discover_sites', return_value=([], [])):
            return setup.collect_config(self.project)

    def test_first_run_collects_valid_config_without_leaking_secrets(self):
        config, environment, password = self.collect()
        self.assertEqual(config['global']['restic']['repository'], 's3:https://' + 'a' * 32 + '.r2.cloudflarestorage.com/backup-bucket/forge-test')
        self.assertFalse(config['sites'][0]['backup_database'])
        self.assertEqual(password, 'password-from-manager')
        self.assertIn("AWS_SECRET_ACCESS_KEY='SECRET'", environment)
        self.assertNotIn('SECRET', self.output.getvalue())
        self.assertNotIn('password-from-manager', self.output.getvalue())
        self.assertFalse((self.project / 'config.yaml').exists())
        self.assertFalse((self.project / 'restic-password').exists())

    def test_new_files_have_private_permissions_and_parse(self):
        config, environment, password = self.collect()
        setup.write_config(self.project, config, environment, password)
        for name in ('config.yaml', 'restic.env', 'restic-password'):
            self.assertEqual((self.project / name).stat().st_mode & 0o777, 0o600)
        self.assertEqual(read_env(self.project / 'restic.env')['AWS_SECRET_ACCESS_KEY'], 'SECRET')
        self.assertEqual((self.project / 'restic-password').read_text(), password + '\n')

    def test_existing_password_is_not_overwritten_and_new_env_rolled_back(self):
        path = self.project / 'restic-password'
        path.write_text('original-password')
        with self.assertRaises(FileExistsError):
            setup.write_config(self.project, {}, 'NEW_ENV', 'NEW_PASSWORD')
        self.assertEqual(path.read_text(), 'original-password')
        self.assertFalse((self.project / 'restic.env').exists())
        self.assertFalse((self.project / 'config.yaml').exists())

    def test_orphaned_secrets_stop_setup(self):
        (self.project / 'restic.env').write_text('existing')
        with patch.object(setup, 'collect_config') as collect, self.assertRaises(BackupError):
            setup.setup(self.project)
        collect.assert_not_called()

    def test_existing_configuration_is_reused_without_prompts_for_secrets(self):
        path = self.project / 'config.yaml'
        path.write_text('existing-configuration')
        with patch.object(setup, 'BackupScript') as app, patch.object(setup, 'ensure_repository') as ensure, patch.object(setup, 'collect_config') as collect, patch.object(setup, 'yes', return_value=False):
            setup.setup(self.project)
        collect.assert_not_called()
        ensure.assert_called_once_with(app.return_value)
        app.return_value.run.assert_not_called()
        self.assertEqual(path.read_text(), 'existing-configuration')

    def test_failed_first_backup_does_not_install_schedule(self):
        (self.project / 'config.yaml').touch()
        with patch.object(setup, 'BackupScript') as app, patch.object(setup, 'ensure_repository'), patch.object(setup, 'yes', return_value=True), patch.object(setup, 'install_schedule') as schedule:
            app.return_value.run.return_value = 1
            with self.assertRaisesRegex(BackupError, 'Backup failed'):
                setup.setup(self.project)
        schedule.assert_not_called()

    def test_initialization_only_for_explicit_missing_repository_status(self):
        work = self.project / 'work'
        work.mkdir()
        app = SimpleNamespace(work_dir=work, reserve=0, settings={'restic': {}})
        for status in (0, 10, 1, 12):
            with self.subTest(status=status), patch.object(setup, 'Restic') as restic, patch.object(setup, 'run_command', return_value=(status, b'')):
                backend = restic.return_value
                backend.base = ['restic']
                backend.env = {}
                if status in (0, 10):
                    setup.ensure_repository(app)
                else:
                    with self.assertRaisesRegex(BackupError, 'will not initialize'):
                        setup.ensure_repository(app)
                if status == 10:
                    backend.run.assert_called_once_with(['init', '--repository-version', '2'])
                else:
                    backend.run.assert_not_called()

    def test_crontab_preserves_other_jobs_and_is_idempotent(self):
        original = 'MAILTO=admin@example.com\n0 2 * * * /usr/bin/true\n'
        updated = setup.update_crontab(original, self.project)
        self.assertTrue(updated.startswith(original))
        self.assertEqual(updated.count('backup.py'), 2)
        self.assertEqual(setup.update_crontab(updated, self.project), updated)

    def test_legacy_cron_job_is_not_duplicated(self):
        with self.assertRaisesRegex(BackupError, 'existing backup.py'):
            setup.update_crontab('0 3 * * * python /old/path/backup.py\n', self.project)

    def test_cron_escapes_spaces_and_percent_signs(self):
        lines = setup.cron_lines(Path('/home/forge/my backups%20'))
        self.assertIn("'/home/forge/my backups\\%20/venv/bin/python'", lines[0])

    def test_unreadable_crontab_is_not_replaced(self):
        result = SimpleNamespace(returncode=1, stdout='', stderr='permission denied')
        with patch('shutil.which', return_value='/usr/bin/crontab'), patch.object(setup.subprocess, 'run', return_value=result) as run:
            with self.assertRaisesRegex(BackupError, 'Cannot read'):
                setup.install_schedule(self.project)
        self.assertEqual(run.call_count, 1)

    def test_install_script_rejects_unknown_option_and_unattended_setup(self):
        project = Path(__file__).resolve().parent.parent
        for args in (['--wrong'], []):
            result = subprocess.run(['bash', str(project / 'install.sh'), *args], capture_output=True, text=True)
            self.assertEqual(result.returncode, 1)
        help_result = subprocess.run(['bash', str(project / 'install.sh'), '--help'], capture_output=True, text=True)
        self.assertEqual(help_result.returncode, 0)
        self.assertIn('--skip-setup', help_result.stdout)


if __name__ == '__main__':
    unittest.main()
