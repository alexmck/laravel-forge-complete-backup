import contextlib
import copy
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml

from backup import load_config
from recovery import write_recovery_guide
from runtime import BackupError
from scripts import setup


class ManagementTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.project = Path(temp.name).resolve()
        self.path = self.project / 'config.yaml'
        self.config = {
            'global': {'server_id': 'server-one', 'work_dir': str(self.project / 'work'),
                       'discord_webhook_url': 'https://discord.example/PRIVATE-WEBHOOK',
                       'restic': {'repository': 's3:https://example.com/bucket/server-one', 'region': 'auto',
                                  'password_file': str(self.project / 'restic-password'),
                                  'environment_file': str(self.project / 'restic.env')}},
            'defaults': {'retention': {'daily': 7, 'weekly': 4, 'monthly': 12}},
            'sites': [{'name': 'app', 'user_path': str(self.project / 'app'),
                       'backup_database': True, 'exclude_patterns': ['cache'],
                       'database': {'password': 'INLINE-SECRET'}}]}
        self.path.write_text(yaml.safe_dump(self.config))
        (self.project / 'restic-password').write_text('REPO-SECRET')
        (self.project / 'restic.env').write_text('AWS_SECRET_ACCESS_KEY=ACCESS-SECRET')
        output = contextlib.redirect_stdout(io.StringIO())
        output.__enter__()
        self.addCleanup(output.__exit__, None, None, None)

    def menu(self, answers):
        with patch('builtins.input', side_effect=answers):
            return setup.manage_existing(self.project)

    def test_menu_exit_does_not_change_configuration(self):
        original = self.path.read_text()
        self.assertFalse(self.menu(['0']))
        self.assertEqual(self.path.read_text(), original)

    def test_retention_saved_with_rollback_and_secrets_preserved(self):
        original = self.path.read_text()
        self.assertFalse(self.menu(['3', '10', '5', '24', 'y', '0']))
        result = yaml.safe_load(self.path.read_text())
        self.assertEqual(result['defaults']['retention'], {'daily': 10, 'weekly': 5, 'monthly': 24})
        self.assertEqual(result['sites'], self.config['sites'])
        self.assertEqual(result['global'], self.config['global'])
        self.assertEqual((self.project / 'config.yaml.previous').read_text(), original)
        for name in ('config.yaml', 'config.yaml.previous', 'RECOVERY.md'):
            self.assertEqual((self.project / name).stat().st_mode & 0o777, 0o600)
        self.assertEqual((self.project / 'restic-password').read_text(), 'REPO-SECRET')

    def test_declined_save_preserves_original(self):
        original = self.path.read_text()
        self.menu(['3', '1', '1', '1', 'n', '0'])
        self.assertEqual(self.path.read_text(), original)
        self.assertFalse((self.project / 'config.yaml.previous').exists())

    def test_retention_overrides_preserved_unless_selected(self):
        self.config['sites'][0]['retention'] = {'daily': 30}
        with patch('builtins.input', side_effect=['7', '4', '12', 'n']):
            setup.edit_retention(self.config)
        self.assertEqual(self.config['sites'][0]['retention'], {'daily': 30})
        with patch('builtins.input', side_effect=['7', '4', '12', 'y']):
            setup.edit_retention(self.config)
        self.assertNotIn('retention', self.config['sites'][0])

    def test_schedule_updates_only_managed_block(self):
        old = setup.update_crontab('0 1 * * * other-job\n', self.project)
        with patch.object(setup, 'install_schedule') as install:
            self.menu(['4', '25:00', '02:15', '06:45', 'y', 'n', '0'])
        install.assert_not_called()
        updated = setup.update_crontab(old, self.project)
        self.assertIn('0 1 * * * other-job', updated)
        self.assertIn('15 2 * * *', updated)
        self.assertIn('45 6 * * *', updated)
        self.assertEqual(updated.count('backup.py'), 2)
        self.assertEqual(setup.update_crontab(updated, self.project), updated)

    def test_new_sites_added_without_resetting_existing_settings(self):
        candidate = {'path': str(self.project / 'new'), 'kind': 'Static',
                     'configs': [], 'deployment': False, 'issue': None}
        existing = copy.deepcopy(self.config['sites'][0])
        with patch.object(setup, 'discover_sites', return_value=([candidate], [])), patch('builtins.input', side_effect=['all', 'y', 'all', 'n']):
            setup.edit_sites(self.project, self.config)
        self.assertEqual(self.config['sites'][0], existing)
        self.assertEqual(self.config['sites'][1]['name'], 'new')
        self.assertFalse(self.config['sites'][1]['backup_database'])

    def test_removed_site_does_not_delete_any_files(self):
        self.config['sites'].append({'name': 'other', 'user_path': '/srv/other'})
        with patch('builtins.input', side_effect=['1', 'n']):
            setup.edit_sites(self.project, self.config)
        self.assertEqual([s['name'] for s in self.config['sites']], ['app'])

    def test_concurrent_edit_and_invalid_draft_do_not_replace_config(self):
        original = self.path.read_text()
        self.path.write_text(original + '\n# another edit\n')
        with self.assertRaisesRegex(BackupError, 'changed during'):
            setup.save_changes(self.project, original, self.config)
        self.config['sites'] = []
        with self.assertRaises(BackupError):
            setup.save_changes(self.project, self.path.read_text(), self.config)
        self.assertEqual(self.path.read_text(), original + '\n# another edit\n')
        self.assertFalse((self.project / 'config.yaml.previous').exists())

    def test_recovery_contains_exact_location_tags_and_no_secrets(self):
        settings, sites = load_config(self.path)
        guide = write_recovery_guide(self.project, settings, sites).read_text()
        self.assertIn('s3:https://example.com/bucket/server-one', guide)
        self.assertIn('forge-backup,server:server-one,site:app,complete', guide)
        self.assertIn('restic restore "$SNAPSHOT_ID" --target "$RESTORE_DIR" --verify', guide)
        self.assertIn(str(self.project / 'work/staging/app/database.sql'), guide)
        for secret in ('INLINE-SECRET', 'REPO-SECRET', 'ACCESS-SECRET', 'PRIVATE-WEBHOOK'):
            self.assertNotIn(secret, guide)

    def test_credential_bearing_repository_not_exported(self):
        settings, sites = load_config(self.path)
        settings['restic']['repository'] = 's3:https://user:secret@example.com/bucket'
        with self.assertRaises(BackupError):
            write_recovery_guide(self.project, settings, sites)
        self.assertFalse((self.project / 'RECOVERY.md').exists())

    def test_regenerate_guide_without_connecting_to_repository(self):
        with patch.object(setup, 'ensure_repository') as connect:
            self.menu(['5', '0'])
        connect.assert_not_called()
        self.assertTrue((self.project / 'RECOVERY.md').exists())


if __name__ == '__main__':
    unittest.main()
