import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import site_discovery as discovery
from runtime import BackupError
from scripts import setup


class DiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.home = self.root / 'home'
        self.nginx = self.root / 'nginx'
        self.home.mkdir()
        self.nginx.mkdir()
        self.project = self.home / 'forge/backup-tool'
        self.project.mkdir(parents=True)
        self.output = io.StringIO()
        redirect = contextlib.redirect_stdout(self.output)
        redirect.__enter__()
        self.addCleanup(redirect.__exit__, None, None, None)

    def file(self, relative, content=''):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
        return path

    def scan(self):
        return discovery.discover_sites(self.project, self.home, self.nginx)

    def test_standard_isolated_and_static_sites_excluding_project(self):
        self.file('home/forge/app.test/artisan')
        self.file('home/blog/blog.test/public/wp-config.php')
        self.file('home/static/public/index.html')
        self.file('home/forge/backup-tool/.env')
        self.file('home/forge/.env')
        self.file('home/forge/vendor/fake/artisan')
        self.file('home/forge/.hidden/artisan')
        candidates, warnings = self.scan()
        self.assertEqual({c['kind'] for c in candidates}, {'Laravel', 'WordPress', 'Static'})
        self.assertEqual(len(candidates), 3)
        self.assertEqual(warnings, [])

    def test_current_paths_follow_deployments_and_nginx_deduplicates(self):
        env = self.file('home/forge/app.test/.env', 'DB_DATABASE=app\n')
        release = self.file('home/forge/app.test/releases/001/artisan').parent
        (release / '.env').symlink_to(env)
        root = env.parent
        (root / 'current').symlink_to(release, target_is_directory=True)
        (release / 'public').mkdir()
        self.file('nginx/app', f'server {{ root {root}/current/public; }}')
        candidates, warnings = self.scan()
        self.assertEqual(len(candidates), 1)
        candidate = candidates[0]
        self.assertEqual(candidate['path'], str(root))
        self.assertEqual(candidate['configs'], [str(root / 'current/.env')])
        self.assertTrue(candidate['deployment'])
        self.assertIsNone(candidate['issue'])
        self.assertEqual(warnings, [])

    def test_custom_nginx_root_and_dynamic_root_notice(self):
        root = self.file('srv/custom site/public/index.php').parent.parent
        self.file('nginx/custom', f'server {{ root "{root}/public"; }}\nserver {{ root /srv/$host; }}')
        candidates, warnings = self.scan()
        self.assertEqual([c['path'] for c in candidates], [str(root)])
        self.assertTrue(any('Dynamic Nginx root' in w for w in warnings))

    def test_external_current_is_flagged(self):
        release = self.file('external/artisan').parent
        root = self.home / 'forge/app.test'
        root.mkdir()
        (root / 'current').symlink_to(release, target_is_directory=True)
        candidates, _ = self.scan()
        self.assertIn('outside', candidates[0]['issue'])

    def test_unreadable_home_reports_notice(self):
        original = discovery.os.scandir
        def scan(path):
            if Path(path) == self.home:
                raise PermissionError('denied')
            return original(path)
        with patch.object(discovery.os, 'scandir', side_effect=scan):
            candidates, warnings = self.scan()
        self.assertEqual(candidates, [])
        self.assertTrue(any('Cannot list' in w for w in warnings))

    def test_selection(self):
        self.assertEqual(setup.selection('all', 3), [0, 1, 2])
        self.assertEqual(setup.selection('manual', 3), [])
        self.assertEqual(setup.selection('1, 3-4 3', 4), [0, 2, 3])
        for value in ('0', '5', '3-1', 'bad', ''):
            with self.subTest(value=value), self.assertRaises(BackupError):
                setup.selection(value, 4)

    def test_detected_database_is_included_without_credentials_prompt(self):
        env = self.file('home/forge/app.test/.env', 'DB_CONNECTION=mysql\nDB_DATABASE=app\nDB_USERNAME=forge\nDB_PASSWORD=secret\n')
        candidate = discovery.inspect_site(env.parent)
        with patch('shutil.which', return_value='/usr/bin/mysqldump'), patch('builtins.input') as prompt:
            site = setup.collect_site({'app.test'}, candidate)
        prompt.assert_not_called()
        self.assertEqual(site['name'], 'app.test-2')
        self.assertTrue(site['backup_database'])
        self.assertEqual(site['database']['config_file'], str(env))
        self.assertNotIn('secret', self.output.getvalue())

    def test_ambiguous_database_requires_choice_or_explicit_none(self):
        env = self.file('home/forge/app.test/.env', 'DB_DATABASE=app\nDB_USERNAME=forge\nDB_PASSWORD=secret\n')
        self.file('home/forge/app.test/public/wp-config.php')
        candidate = discovery.inspect_site(env.parent)
        with patch('shutil.which', return_value='/usr/bin/mysqldump'), patch('builtins.input', return_value='1') as prompt:
            site = setup.collect_site(set(), candidate)
        self.assertEqual(prompt.call_count, 1)
        self.assertEqual(site['database']['config_file'], str(env))
        with patch('shutil.which', return_value='/usr/bin/mysqldump'), patch('builtins.input', return_value='none'):
            site = setup.collect_site(set(), candidate)
        self.assertFalse(site['backup_database'])
        self.assertNotIn('database', site)

    def test_wizard_only_includes_selected_sites(self):
        self.file('home/forge/a.test/index.html')
        self.file('home/forge/b.test/index.html')
        candidates, _ = self.scan()
        with patch.object(setup, 'discover_sites', return_value=(candidates, [])), patch('builtins.input', side_effect=['2', 'n']):
            sites = setup.collect_sites(self.project)
        self.assertEqual([s['name'] for s in sites], ['b.test'])
        self.assertFalse(sites[0]['backup_database'])


if __name__ == '__main__':
    unittest.main()
