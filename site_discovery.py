"""Bounded, read-only discovery of common Forge filesystem and Nginx layouts."""
import os
import re
from pathlib import Path

CONFIG_NAMES = ('.env', 'wp-config.php', 'LocalSettings.php', 'conf_global.php')
SKIP_DIRECTORIES = {'vendor', 'node_modules', 'releases', 'shared', 'backups', 'venv', 'bin'}


def deployment_root(path):
    """Keep the stable site root, including releases and shared files."""
    path = Path(path).absolute()
    if path.name == 'public':
        path = path.parent
    if path.name == 'current' and (path.is_symlink() or (path.parent / 'releases').is_dir()):
        path = path.parent
    return path.resolve()


def config_files(root):
    # Preserve the lexical current path in saved config: it must follow future deploys.
    bases = [root / 'current', root, root / 'shared'] if (root / 'current').is_dir() else [root]
    found = []
    targets = set()
    for base in bases:
        for prefix in (base, base / 'public'):
            for name in CONFIG_NAMES:
                path = prefix / name
                if path.is_file() and path.resolve() not in targets:
                    targets.add(path.resolve())
                    found.append(str(path))
    return found


def inspect_site(path, *, force=False, project=None):
    root = deployment_root(path)
    if project and (root == Path(project).resolve() or Path(project).resolve() in root.parents):
        return None
    if not root.is_dir():
        return None
    active = root / 'current' if (root / 'current').is_dir() else root
    configs = config_files(root)
    if (active / 'artisan').is_file():
        kind = 'Laravel'
    elif any(Path(p).name == 'wp-config.php' for p in configs):
        kind = 'WordPress'
    elif any(Path(p).name == 'LocalSettings.php' for p in configs):
        kind = 'MediaWiki'
    elif any(Path(p).name == 'conf_global.php' for p in configs):
        kind = 'Invision'
    elif any((active / p).is_file() for p in ('index.php', 'public/index.php')):
        kind = 'PHP'
    elif configs:
        kind = 'Application'
    elif any((active / p).is_file() for p in ('index.html', 'public/index.html')):
        kind = 'Static'
    elif force:
        kind = 'Site'
    else:
        return None
    issue = None
    if active != root and not active.resolve().is_relative_to(root):
        issue = 'current points outside the site directory; select a root containing the actual release and shared files manually'
    try:
        with os.scandir(root) as entries:
            next(entries, None)
    except OSError:
        issue = 'the backup user cannot read this directory'
    return {'path': str(root), 'kind': kind, 'configs': configs,
            'deployment': active != root, 'issue': issue}


def discover_sites(project, home_root=Path('/home'), nginx_dir=Path('/etc/nginx/sites-enabled')):
    """Inspect /home/<user> and one site level below it, plus literal Nginx roots.

    Never recurse through uploads, vendor, releases, or directory symlinks. No PHP
    or Nginx commands are executed and file contents are never returned to the UI.
    """
    found = {}
    warnings = set()
    project = Path(project).resolve()

    def inspect(path, force=False):
        try:
            candidate = inspect_site(path, force=force, project=project)
            if candidate:
                found[candidate['path']] = candidate
            return candidate
        except (OSError, RuntimeError):
            warnings.add(f'Could not inspect {path}')
            return None

    def directories(path):
        result = []
        try:
            with os.scandir(path) as entries:
                for index, entry in enumerate(entries):
                    if index >= 2000:
                        warnings.add(f'Scan limit reached in {path}; additional sites can be entered manually')
                        break
                    if entry.name.startswith('.') or entry.name in SKIP_DIRECTORIES:
                        continue
                    if entry.is_dir(follow_symlinks=False):
                        result.append(Path(entry.path))
        except OSError:
            warnings.add(f'Cannot list {path}; check the backup user’s permissions')
        return sorted(result)

    if Path(home_root).exists():
        for user_home in directories(home_root):
            candidate = inspect(user_home)
            if candidate and candidate['kind'] != 'Application':
                continue
            if candidate:
                # A user's incidental .env is not enough to back up their whole home.
                found.pop(candidate['path'], None)
            for site_dir in directories(user_home):
                inspect(site_dir)

    if Path(nginx_dir).is_dir():
        try:
            with os.scandir(nginx_dir) as entries:
                for index, entry in enumerate(entries):
                    if index >= 1000:
                        warnings.add('Nginx scan limit reached; additional sites can be entered manually')
                        break
                    path = Path(entry.path)
                    try:
                        if not path.is_file():
                            continue
                        with path.open() as stream:
                            content = stream.read(524289)
                        if len(content) > 524288:
                            warnings.add(f'Skipped unusually large Nginx config: {path.name}')
                            continue
                        # Only literal root directives; includes, variables and aliases
                        # are not evaluated. Home-directory discovery is independent.
                        content = re.sub(r'#.*', '', content)
                        for match in re.finditer(r'''(?:^|[;{}])\s*root\s+(?:"([^"\n]+)"|'([^'\n]+)'|([^\s;]+))\s*;''', content, re.M):
                            value = next(part for part in match.groups() if part is not None)
                            if not value.startswith('/') or any(char in value for char in '$*?'):
                                warnings.add(f'Dynamic Nginx root in {path.name}; enter that site manually if missing')
                                continue
                            root = Path(value)
                            if not inspect(root, force=True):
                                warnings.add(f'Nginx site root is unavailable: {root}')
                    except (OSError, UnicodeError):
                        warnings.add(f'Cannot read Nginx config: {path.name}')
        except OSError:
            warnings.add(f'Cannot list {nginx_dir}; using filesystem discovery only')
    # A deployment discovered via current/public and its containing root is one site.
    candidates = sorted(found.values(), key=lambda item: item['path'])
    return candidates, sorted(warnings)
