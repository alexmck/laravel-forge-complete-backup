"""Installation-specific recovery instructions, without stored credentials."""
import os
import shlex
import tempfile
from pathlib import Path
from urllib.parse import urlsplit

from runtime import BackupError


def atomic_private_write(path, text):
    path = Path(path)
    with tempfile.NamedTemporaryFile(mode='w', dir=path.parent, delete=False) as stream:
        temporary = Path(stream.name)
        try:
            os.chmod(temporary, 0o600)
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
    try:
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def write_recovery_guide(project, settings, sites):
    repository = settings['restic']['repository']
    remote = repository.startswith('s3:')
    if remote:
        endpoint = urlsplit(repository[3:])
        if endpoint.scheme != 'https' or not endpoint.hostname or endpoint.username or endpoint.password or endpoint.query or endpoint.fragment:
            raise BackupError('Recovery guide requires an HTTPS S3 repository address without embedded credentials or query parameters')
    elif not repository.startswith('/'):
        raise BackupError('Recovery guide supports HTTPS S3 repositories and absolute local repository paths')
    region = settings['restic'].get('region', 'auto')
    if any(c in repository + region for c in '\r\n`'):
        raise BackupError('Repository address or region cannot be safely included in the recovery guide')
    lines = [
        '# Backup recovery guide', '',
        'Keep a copy off the server, alongside the repository password in your password manager.',
        'This file contains repository and site locations, but no passwords, access keys or webhook URLs.', '',
        '## 1. Prepare a recovery computer', '',
        'Install restic 0.19.1 or newer. On macOS: `brew install restic`.',
        'Use Bash for the commands below. Retrieve the original repository password from your password manager.',
        'The password is essential: losing it makes the encrypted backups unrecoverable.',
    ]
    if remote:
        lines += ['Obtain S3 credentials with read access to this bucket and repository prefix.',
                  'For R2, you may create replacement credentials if the originals were lost.']
    else:
        lines += ['Make the local repository available at the path below, or adjust the path to its recovered location.']
    lines += ['', '## 2. Connect without putting secrets in shell history', '', '```bash',
              'umask 077', 'unset RESTIC_PASSWORD_FILE RESTIC_PASSWORD_COMMAND RESTIC_REPOSITORY_FILE',
              f'export RESTIC_REPOSITORY={shlex.quote(repository)}']
    if remote:
        lines += ['unset AWS_SESSION_TOKEN', f'export AWS_DEFAULT_REGION={shlex.quote(region)}',
                  'read -r -s -p "S3 access key ID: " AWS_ACCESS_KEY_ID; printf "\\n"',
                  'read -r -s -p "S3 secret access key: " AWS_SECRET_ACCESS_KEY; printf "\\n"',
                  'export AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY']
    lines += ['read -r -s -p "Repository password: " RESTIC_PASSWORD; printf "\\n"',
              'export RESTIC_PASSWORD', '```', '',
              '## 3. List complete backups', '', '```bash']
    for site in sites:
        tags = f"forge-backup,server:{settings['server_id']},site:{site['name']},complete"
        lines.append('restic snapshots --tag ' + shlex.quote(tags))
    lines += ['```', '', 'Choose an exact snapshot ID from the list for the site you want.', '',
              '## 4. Download, decrypt and verify', '', '```bash',
              'read -r -p "Snapshot ID: " SNAPSHOT_ID',
              'RESTORE_DIR=$(mktemp -d "$HOME/forge-restore.XXXXXX")',
              'restic restore "$SNAPSHOT_ID" --target "$RESTORE_DIR" --verify',
              'printf "Restored files: %s\\n" "$RESTORE_DIR"', '```', '',
              'Proceed only if restic exits successfully. This restores files into a new private directory;',
              'it does not deploy a site or import SQL. Restored files are decrypted and may contain secrets.', '',
              'Original absolute paths are recreated beneath the restore directory:', '']
    for site in sites:
        lines.append(f"- **{site['name']}**: `{site['user_path']}`")
        if site['backup_database']:
            stage = Path(settings['work_dir']) / 'staging' / site['name']
            lines.append(f'  SQL and dump metadata: `{stage}/database.sql` and `{stage}/manifest.json`.')
    lines += ['', 'Verification checks downloaded file contents; it does not prove that SQL can be imported.',
              'Inspect the files first. Database import and application deployment are separate recovery steps.', '',
              '## 5. Clear credentials from this shell', '', '```bash',
              'unset RESTIC_PASSWORD AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AWS_SESSION_TOKEN', '```', '']
    destination = Path(project) / 'RECOVERY.md'
    atomic_private_write(destination, '\n'.join(lines))
    return destination
