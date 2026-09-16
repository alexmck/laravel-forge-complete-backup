"""Compact Discord embeds shared by site events and run summaries."""
from datetime import datetime, timezone

COLORS = {'success': 0x2ECC71, 'warning': 0xF1C40F, 'error': 0xE74C3C}


def text_length(value):
    return len(str(value).encode('utf-16-le')) // 2


def clip(value, limit):
    value = str(value)
    if text_length(value) <= limit:
        return value
    return value.encode('utf-16-le')[:(limit - 1) * 2].decode('utf-16-le', errors='ignore') + '…'


def duration(seconds):
    seconds = max(0, int(seconds))
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return f'{hours}h {minutes:02d}m {seconds:02d}s'
    return f'{minutes}m {seconds:02d}s' if minutes else f'{seconds}s'


def byte_size(count):
    count = float(count)
    for unit in ('B', 'KiB', 'MiB', 'GiB', 'TiB'):
        if count < 1024 or unit == 'TiB':
            return f'{count:.1f} {unit}'
        count /= 1024


def field(name, value, inline=True):
    return {'name': name, 'value': str(value), 'inline': inline}


def notification(title, description, status, fields, category):
    embed = {
        'author': {'name': category},
        'title': clip(title, 256), 'description': clip(description, 1000),
        'color': COLORS[status], 'fields': [],
        'timestamp': datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z'),
        'footer': {'text': 'Forge Backups • restic'},
    }
    used = sum(text_length(value) for value in (
        embed['title'], embed['description'], category, embed['footer']['text']))
    # Stay below both per-field and combined Discord embed limits.
    for item in fields[:25]:
        name = clip(item['name'], 256) or '\u200b'
        available = min(1024, 5800 - used - text_length(name))
        if available < 1:
            break
        value = clip(item['value'], available) or '\u200b'
        embed['fields'].append(field(name, value, item.get('inline', True)))
        used += text_length(name) + text_length(value)
    return {'username': 'Forge Backups', 'allowed_mentions': {'parse': []}, 'embeds': [embed]}


def site_notification(server, site, status, elapsed, *, snapshot=None, database=None,
                      stats=None, error=None, phase=None):
    fields = [field('Site', site['name']), field('Server', server),
              field('Elapsed', duration(elapsed))]
    if status == 'error':
        title = '❌ Backup failed'
        description = 'This site did not complete a successful backup.'
        fields += [field('Failed stage', phase or 'Backup', False),
                   field('Reason', error or 'Unknown error', False)]
    else:
        title = '✅ Backup complete' if status == 'success' else '⚠️ Backup saved · retention failed'
        description = ('Files and validated SQL saved to restic.' if site['backup_database']
                       else 'Site files saved to restic. Database backup is disabled.')
        sql = 'Disabled · files only'
        if site['backup_database']:
            sql = 'Validated'
            if database and 'bytes' in database:
                sql += f" · {byte_size(database['bytes'])}"
        policy = site['retention']
        fields.append(field('Database size', sql))
        stats = stats or {}
        for label, key, detail in (
                ('Total files processed', 'total_bytes_processed', 'Files + SQL + metadata · uncompressed'),
                ('Data uploaded', 'data_added_packed', 'New data added · compressed')):
            count = stats.get(key)
            value = byte_size(count) if type(count) is int and count >= 0 else 'Unavailable'
            fields.append(field(label, f'{value}\n{detail}'))
        fields.append(field('Retention', 'Needs attention' if status == 'warning' else
                            f"{policy['daily']} daily · {policy['weekly']} weekly · {policy['monthly']} monthly"))
        if snapshot:
            fields.append(field('Snapshot', f'`{snapshot}`', False))
        if error:
            fields.append(field('Retention error', error, False))
    return notification(title, description, status, fields, 'SITE BACKUP')


def site_list(names):
    # Bound large configurations without producing a half-cut site name.
    lines = []
    for index, name in enumerate(names):
        line = clip(name, 180)
        if len(lines) == 8 or text_length('\n'.join(lines + [line])) > 900:
            lines.append(f'… and {len(names) - index} more')
            break
        lines.append(line)
    return '\n'.join(lines)


def summary_notification(server, sites, results, elapsed):
    successful = sum(saved for saved, _ in results)
    failed = [site['name'] for site, (saved, _) in zip(sites, results) if not saved]
    retention = [site['name'] for site, (_, retained) in zip(sites, results) if not retained]
    status = 'error' if failed and not successful else 'warning' if failed or retention else 'success'
    fields = [field('Successful', successful), field('Failed', len(failed)),
              field('Retention issues', len(retention)), field('Server', server),
              field('Elapsed', duration(elapsed))]
    if failed:
        fields.append(field('Failed sites', site_list(failed), False))
    if retention:
        fields.append(field('Retention needs attention', site_list(retention), False))
    description = (f'All {len(sites)} sites backed up successfully.' if status == 'success'
                   else f'{successful} of {len(sites)} sites backed up. Review the issues below.')
    return notification('✅ Backup run complete' if status == 'success' else '⚠️ Backup run needs attention',
                        description, status, fields, 'RUN SUMMARY')


def job_failure(server, command, error, elapsed):
    label = {'backup': 'Backup', 'maintenance': 'Maintenance', 'check': 'Integrity check',
             'init': 'Repository initialization'}[command]
    return notification(f'❌ {label} job failed', 'The job stopped before it could finish.',
                        'error', [field('Server', server), field('Job', label),
                                  field('Elapsed', duration(elapsed)), field('Reason', error, False)],
                        'JOB ALERT')
