import unittest
from unittest.mock import Mock, patch

import requests

from backup import BackupScript
from notifications import COLORS, duration, job_failure, notification, site_notification, summary_notification, text_length


class NotificationTests(unittest.TestCase):
    def setUp(self):
        self.site = {'name': 'website1-com', 'backup_database': True,
                     'retention': {'daily': 7, 'weekly': 4, 'monthly': 12}}

    def test_success_fields(self):
        payload = site_notification('forge-1', self.site, 'success', 83,
                                    snapshot='a' * 64, database={'bytes': 1048576})
        embed = payload['embeds'][0]
        values = {item['name']: item['value'] for item in embed['fields']}
        self.assertEqual(values['Elapsed'], '1m 23s')
        self.assertEqual(values['Database'], 'Validated · 1.0 MiB')
        self.assertEqual(values['Snapshot'], '`' + 'a' * 64 + '`')
        self.assertEqual(embed['color'], COLORS['success'])
        self.assertEqual(payload['allowed_mentions'], {'parse': []})
        self.assertTrue(embed['timestamp'].endswith('Z'))

    def test_files_only_does_not_claim_validated_database(self):
        self.site['backup_database'] = False
        embed = site_notification('forge-1', self.site, 'success', 1)['embeds'][0]
        self.assertNotIn('validated', embed['description'])
        self.assertIn('Disabled · files only', [f['value'] for f in embed['fields']])

    def test_retention_warning_preserves_snapshot(self):
        embed = site_notification('forge-1', self.site, 'warning', 90,
                                  snapshot='abc12345', error='Retention failed')['embeds'][0]
        self.assertEqual(embed['color'], COLORS['warning'])
        self.assertIn('saved', embed['title'])
        self.assertIn('Snapshot', [f['name'] for f in embed['fields']])

    def test_failed_stage(self):
        embed = site_notification('forge-1', self.site, 'error', 10,
                                  error='Dump failed', phase='Database dump and validation')['embeds'][0]
        self.assertEqual(embed['color'], COLORS['error'])
        self.assertNotIn('Snapshot', [f['name'] for f in embed['fields']])
        self.assertIn('Database dump and validation', [f['value'] for f in embed['fields']])

    def test_summary_names_failures_and_retention_issues(self):
        sites = [self.site, {**self.site, 'name': 'other'}, {**self.site, 'name': 'third'}]
        embed = summary_notification('forge-1', sites, [(True, True), (False, True), (True, False)], 3700)['embeds'][0]
        fields = {f['name']: f['value'] for f in embed['fields']}
        self.assertEqual(fields['Successful'], '2')
        self.assertEqual(fields['Failed sites'], 'other')
        self.assertEqual(fields['Retention needs attention'], 'third')
        self.assertEqual(fields['Elapsed'], '1h 01m 40s')

    def test_everything_failed_is_red(self):
        embed = summary_notification('forge-1', [self.site], [(False, True)], 5)['embeds'][0]
        self.assertEqual(embed['color'], COLORS['error'])

    def test_maintenance_failure_has_accurate_job_label(self):
        embed = job_failure('forge-1', 'maintenance', 'Check failed', 4)['embeds'][0]
        self.assertIn('Maintenance', embed['title'])

    def test_discord_limits_with_long_unicode_values(self):
        embed = notification('🛠' * 1000, '🛠' * 5000, 'error',
                             [{'name': '🛠' * 300, 'value': '🛠' * 2000} for _ in range(30)], 'JOB ALERT')['embeds'][0]
        self.assertLessEqual(len(embed['fields']), 25)
        size = sum(text_length(v) for v in [embed['title'], embed['description'], embed['author']['name'], embed['footer']['text']])
        for field in embed['fields']:
            self.assertLessEqual(text_length(field['name']), 256)
            self.assertLessEqual(text_length(field['value']), 1024)
            size += text_length(field['name']) + text_length(field['value'])
        self.assertLessEqual(size, 6000)

    def test_large_summary_truncates_site_list(self):
        sites = [{**self.site, 'name': f'site-{i}'} for i in range(100)]
        embed = summary_notification('forge-1', sites, [(False, True)] * 100, 0)['embeds'][0]
        failed = next(f['value'] for f in embed['fields'] if f['name'] == 'Failed sites')
        self.assertIn('92 more', failed)

    def test_webhook_failure_does_not_raise_or_log_secret_url(self):
        app = BackupScript.__new__(BackupScript)
        app.settings = {'discord_webhook_url': 'https://example.invalid/secret-token'}
        with patch('backup.requests.post', side_effect=requests.RequestException('secret-token')), self.assertLogs(level='WARNING') as logs:
            app.notify({'embeds': []})
        self.assertNotIn('secret-token', '\n'.join(logs.output))

    def test_disabled_webhook_does_not_send(self):
        app = BackupScript.__new__(BackupScript)
        app.settings = {}
        with patch('backup.requests.post') as post:
            app.notify({'embeds': []})
            post.assert_not_called()
