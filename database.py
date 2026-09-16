"""Read static application credentials and create validated MySQL/MariaDB dumps."""
import hashlib
import os
import re
import shutil
import tempfile
from pathlib import Path

from dotenv.parser import parse_stream

from runtime import BackupError, check_space, protected_file, run_command


PHP_STRING = r'''(?:'(?:\\.|[^'\\])*'|"(?:\\.|[^"\\])*")'''


def php_literal(token):
    body = token[1:-1]
    if token[0] == "'":
        return re.sub(r"\\([\\'])", r"\1", body)
    # PHP interpolation and less common escape forms require an explicit override.
    if re.search(r"(?<!\\)\$|\\(?:[0-7xXuU])", body):
        raise BackupError("Dynamic PHP database settings require an explicit database override")
    escapes = {'n': '\n', 'r': '\r', 't': '\t', 'v': '\v', 'e': '\x1b',
               'f': '\f', '\\': '\\', '"': '"', '$': '$'}
    return re.sub(r'\\(.)', lambda m: escapes.get(m[1], '\\' + m[1]), body)


def read_env(path):
    values = {}
    with Path(path).open() as stream:
        for binding in parse_stream(stream):
            if binding.error:
                raise BackupError("Invalid dotenv syntax; use a static database override if needed")
            if binding.key:
                values[binding.key] = binding.value
    return values


def discover(site_path, source=None):
    candidates = [Path(source)] if source else [
        site_path / '.env', site_path / 'public/wp-config.php',
        site_path / 'public/LocalSettings.php', site_path / 'public/conf_global.php',
    ]
    existing = [p for p in candidates if p.is_file()]
    if len(existing) != 1:
        raise BackupError("Database discovery requires exactly one config file; set database.config_file or explicit credentials")
    path = existing[0]
    if path.name == '.env':
        values = read_env(path)
        if values.get('DB_CONNECTION', 'mysql') not in ('mysql', 'mariadb'):
            raise BackupError("Only MySQL/MariaDB databases are supported")
        if values.get('DB_URL') or values.get('DATABASE_URL'):
            raise BackupError("Database URLs require explicit database credentials")
        result = {key: values.get(env) for key, env in {
            'host': 'DB_HOST', 'port': 'DB_PORT', 'name': 'DB_DATABASE',
            'user': 'DB_USERNAME', 'password': 'DB_PASSWORD', 'socket': 'DB_SOCKET',
        }.items() if values.get(env) is not None}
        if any('${' in str(value) for value in result.values()):
            raise BackupError("Interpolated dotenv credentials require an explicit database override")
        return result
    content = path.read_text()
    # Strip comments without changing quoted values. Never execute application PHP.
    content = re.sub(PHP_STRING + r'|/\*[\s\S]*?\*/|//[^\n]*|\#[^\n]*',
                     lambda m: m[0] if m[0][0] in "'\"" else '', content)
    patterns = {
        'wp-config.php': ('name', 'user', 'password', 'host'),
        'LocalSettings.php': ('name', 'user', 'password', 'host'),
        'conf_global.php': ('name', 'user', 'password', 'host', 'port'),
    }
    if path.name not in patterns:
        raise BackupError("Unsupported database config filename; use explicit credentials")
    result = {}
    for field in patterns[path.name]:
        if path.name == 'wp-config.php':
            key = {'name': 'DB_NAME', 'user': 'DB_USER', 'password': 'DB_PASSWORD', 'host': 'DB_HOST'}[field]
            pattern = rf'''define\s*\(\s*['"]{key}['"]\s*,\s*({PHP_STRING})\s*\)\s*;'''
        elif path.name == 'LocalSettings.php':
            key = {'name': 'wgDBname', 'user': 'wgDBuser', 'password': 'wgDBpassword', 'host': 'wgDBserver'}[field]
            pattern = rf'\${key}\s*=\s*({PHP_STRING})\s*;'
        else:
            key = {'name': 'sql_database', 'user': 'sql_user', 'password': 'sql_pass', 'host': 'sql_host', 'port': 'sql_port'}[field]
            pattern = rf'''(?:\$INFO\s*\[\s*['"]{key}['"]\s*\]\s*=\s*({PHP_STRING})\s*;|['"]{key}['"]\s*=>\s*({PHP_STRING})\s*(?=,|\)))'''
        matches = list(re.finditer(pattern, content))
        if len(matches) > 1:
            raise BackupError("Ambiguous PHP database settings require an explicit override")
        if matches:
            result[field] = php_literal(next(v for v in matches[0].groups() if v is not None))
    # Missing host/password may indicate an expression, not an intentional default.
    if not all(key in result for key in ('name', 'user', 'password', 'host')):
        raise BackupError("Incomplete or dynamic PHP database settings; use an explicit override")
    return result


def credentials(site):
    override = site.get('database', {})
    if override.get('option_file'):
        if not override.get('name'):
            raise BackupError("database.name is required with database.option_file")
        return {'name': str(override['name']), 'option_file': protected_file(override['option_file'])}
    explicit = all(k in override for k in ('name', 'user', 'password'))
    config = {} if explicit else discover(Path(site['user_path']), override.get('config_file'))
    config.update({k: str(v) for k, v in override.items()
                   if k in ('name', 'user', 'password', 'host', 'port', 'socket')})
    if not config.get('name') or not config.get('user') or 'password' not in config:
        raise BackupError("Database name, user and password are required (empty password is allowed)")
    config.setdefault('host', 'localhost')
    config.setdefault('port', '3306')
    host = config['host']
    if host.count(':') == 1:
        host, suffix = host.split(':', 1)
        if suffix.isdigit():
            config.update(host=host, port=suffix)
            if host == 'localhost' and not config.get('socket'):
                config['host'] = '127.0.0.1'  # An explicit host:port requests TCP.
        elif suffix.startswith('/'):
            config.update(host=host, socket=suffix)
        else:
            raise BackupError("Invalid database host:port or host:socket")
    if not str(config['port']).isdigit() or not 1 <= int(config['port']) <= 65535:
        raise BackupError("Invalid database port")
    if config['host'] == 'localhost' and int(config['port']) != 3306 and not config.get('socket'):
        # Laravel commonly sets DB_PORT separately; localhost would ignore it.
        config['host'] = '127.0.0.1'
    return config


def option_value(value):
    value = str(value)
    if '\x00' in value:
        raise BackupError("NUL bytes are not allowed in database credentials")
    return '"' + value.replace('\\', '\\\\').replace('"', '\\"').replace('\n', '\\n').replace('\r', '\\r').replace('\t', '\\t') + '"'


def dump_database(site, stage, secret_dir, reserve_bytes, timeout=1800):
    config = credentials(site)
    executable = site.get('database', {}).get('dump_binary', 'mysqldump')
    if not shutil.which(executable):
        raise BackupError("Database dump executable is missing")
    if str(config['name']).startswith('-') or '\x00' in config['name']:
        raise BackupError("Invalid database name")
    generated = None
    partial = stage / 'database.sql.partial'
    final = stage / 'database.sql'
    try:
        if config.get('option_file'):
            option_file = config['option_file']
        else:
            fd, filename = tempfile.mkstemp(prefix='mysql-', suffix='.cnf', dir=secret_dir)
            generated = option_file = Path(filename)
            with os.fdopen(fd, 'w') as stream:
                stream.write('[client]\n')
                for field in ('host', 'port', 'user', 'password', 'socket'):
                    if config.get(field) is not None and (field != 'socket' or config[field]):
                        stream.write(f'{field}={option_value(config[field])}\n')
        # --defaults-file is first: do not inherit unrelated user/system defaults.
        env = os.environ.copy()
        env.pop('MYSQL_PWD', None)
        env['MYSQL_TEST_LOGIN_FILE'] = os.devnull
        args = [executable, f'--defaults-file={option_file}', '--single-transaction',
                '--quick', '--skip-force', '--routines', '--triggers', '--events', '--hex-blob',
                '--no-tablespaces', '--comments', '--dump-date']
        status, version = run_command([executable, '--version'], env=env, timeout=30)
        if status:
            raise BackupError("Could not determine database dump client version")
        if b'MariaDB' not in version and b'mariadb' not in version:
            args.append('--set-gtid-purged=OFF')
        args.append(config['name'])
        digest = hashlib.sha256()
        tail = bytearray()
        size = 0
        check_space(stage, reserve_bytes)
        with partial.open('xb') as stream:
            os.chmod(partial, 0o600)
            def write(chunk):
                nonlocal size
                check_space(stage, reserve_bytes, len(chunk))
                stream.write(chunk)
                digest.update(chunk)
                size += len(chunk)
                tail.extend(chunk)
                del tail[:-8192]
            status, _ = run_command(args, env=env, timeout=timeout, output=write,
                                    space_path=stage, reserve_bytes=reserve_bytes)
        if status:
            raise BackupError(f"Database dump failed (exit {status}); check credentials, connectivity and dump privileges")
        if not size or not re.search(rb'(?m)^-- Dump completed on [^\r\n]+\s*$', bytes(tail)):
            raise BackupError("Database dump is empty or missing its completion marker")
        partial.replace(final)
        return {'file': str(final), 'name': config['name'], 'bytes': size, 'sha256': digest.hexdigest()}
    finally:
        partial.unlink(missing_ok=True)
        if generated:
            generated.unlink(missing_ok=True)
