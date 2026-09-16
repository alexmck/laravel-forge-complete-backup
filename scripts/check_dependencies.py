#!/usr/bin/env python3
"""Read-only prerequisite checks; runs before third-party packages are installed."""
import argparse
import importlib.util
import shutil
import subprocess
import sys
from pathlib import Path


def check(project, *, skip_restic=False):
    errors = []
    notices = []
    if sys.version_info < (3, 10):
        errors.append('Python 3.10+ is required. Select a newer interpreter with PYTHON_BIN; do not replace system Python.')
    for module in ('ssl', 'bz2'):
        if importlib.util.find_spec(module) is None:
            errors.append(f'The selected Python is missing {module} support; install a complete Python distribution.')
    interpreter = project / 'venv/bin/python'
    if interpreter.exists():
        try:
            result = subprocess.run(
                [str(interpreter), '-c', 'import sys, pip, ssl; sys.exit(0 if sys.version_info >= (3, 10) else 1)'],
                capture_output=True, timeout=30)
            if result.returncode:
                errors.append('Existing venv needs Python 3.10+, pip and SSL support. Move venv aside and rerun the installer to recreate it.')
        except (OSError, subprocess.SubprocessError):
            errors.append('Existing venv cannot be executed. Check permissions or move it aside and rerun the installer.')
    elif any(importlib.util.find_spec(module) is None for module in ('venv', 'ensurepip')):
        version = f'{sys.version_info.major}.{sys.version_info.minor}'
        errors.append(
            'Python virtual-environment support is missing. For Ubuntu system Python, run: '
            'sudo apt-get install python3-venv. For a separately installed Python, install '
            f'its matching venv support (typically python{version}-venv), then rerun.')
    if not (shutil.which('mysqldump') or shutil.which('mariadb-dump')):
        notices.append(
            'No MySQL/MariaDB dump client found on PATH. Database backups require one; '
            'files-only backups do not. Install a client matching your database: '
            'sudo apt-get install mysql-client OR sudo apt-get install mariadb-client. '
            'A configured absolute dump_binary path may also be used.')
    if not shutil.which('crontab'):
        notices.append('crontab is unavailable. Use Forge scheduling, or install cron: sudo apt-get install cron.')
    if skip_restic:
        binary = project / 'bin/restic'
        if not binary.is_file() and not shutil.which('restic'):
            errors.append('Restic is missing. Rerun without --skip-restic to install it automatically, or put restic on PATH.')
    return errors, notices


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--skip-restic', action='store_true')
    args = parser.parse_args()
    errors, notices = check(Path(__file__).resolve().parent.parent, skip_restic=args.skip_restic)
    print('Dependency check:')
    for notice in notices:
        print(f'  Notice: {notice}')
    for error in errors:
        print(f'  Missing requirement: {error}')
    if errors:
        print('Resolve the requirements above, then rerun install.sh.')
        return 1
    print('  Python prerequisites available. Restic and project libraries are handled by the installer.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
