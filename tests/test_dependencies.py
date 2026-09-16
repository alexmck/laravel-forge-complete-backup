import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import check_dependencies as dependencies


class DependencyTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.project = Path(temporary.name)

    def check(self, modules=None, commands=None, **kwargs):
        with patch.object(dependencies.importlib.util, 'find_spec', side_effect=lambda name: None if name in (modules or []) else object()), patch.object(dependencies.shutil, 'which', side_effect=lambda name: '/usr/bin/' + name if name in (commands or []) else None):
            return dependencies.check(self.project, **kwargs)

    def test_missing_optional_tools_do_not_block_files_only_install(self):
        errors, notices = self.check()
        self.assertEqual(errors, [])
        self.assertEqual(len(notices), 2)
        self.assertIn('mysql-client OR', notices[0])
        self.assertIn('Forge scheduling', notices[1])

    def test_missing_venv_stops_before_installation_with_remedy(self):
        for module in ('venv', 'ensurepip'):
            with self.subTest(module=module):
                errors, _ = self.check(modules=[module])
                self.assertEqual(len(errors), 1)
                self.assertIn('sudo apt-get install python3-venv', errors[0])

    def test_either_database_client_satisfies_check(self):
        for client in ('mysqldump', 'mariadb-dump'):
            with self.subTest(client=client):
                errors, notices = self.check(commands=[client, 'crontab'])
                self.assertEqual((errors, notices), ([], []))

    def test_existing_working_venv_does_not_require_system_ensurepip(self):
        interpreter = self.project / 'venv/bin/python'
        interpreter.parent.mkdir(parents=True)
        interpreter.touch()
        with patch.object(dependencies.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0)):
            errors, _ = self.check(modules=['ensurepip'])
        self.assertEqual(errors, [])

    def test_broken_existing_venv_has_recovery_instructions(self):
        interpreter = self.project / 'venv/bin/python'
        interpreter.parent.mkdir(parents=True)
        interpreter.touch()
        for outcome in (subprocess.CompletedProcess([], 1), PermissionError()):
            with self.subTest(outcome=outcome):
                arguments = {'side_effect': outcome} if isinstance(outcome, Exception) else {'return_value': outcome}
                with patch.object(dependencies.subprocess, 'run', **arguments):
                    errors, _ = self.check()
                self.assertEqual(len(errors), 1)
                self.assertIn('venv', errors[0])

    def test_skip_restic_requires_existing_binary(self):
        errors, _ = self.check(skip_restic=True)
        self.assertIn('Restic is missing', errors[0])
        errors, _ = self.check(skip_restic=True, commands=['restic'])
        self.assertEqual(errors, [])


if __name__ == '__main__':
    unittest.main()
