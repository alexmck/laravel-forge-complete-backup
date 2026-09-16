"""Bounded subprocess execution and private local state for backup jobs."""
import os
import selectors
import shutil
import signal
import subprocess
import time
from pathlib import Path


class BackupError(Exception):
    """A safe-to-display operational error (never include command output/secrets)."""


def private_directory(path):
    path = Path(path)
    if path.is_symlink():
        raise BackupError("Private directory must not be a symlink")
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.stat().st_uid != os.getuid():
        raise BackupError("Private directory must be owned by the backup user")
    path.chmod(0o700)
    return path


def protected_file(path):
    path = Path(path)
    if not path.is_file() or path.stat().st_mode & 0o077:
        raise BackupError("Secret files must exist and have permissions 0600 or 0400")
    return path


def check_space(path, reserve_bytes, incoming=0):
    if shutil.disk_usage(path).free < reserve_bytes + incoming:
        raise BackupError("Free disk space is below the configured reserve")


def run_command(args, *, env=None, timeout=3600, output=None, space_path=None,
                reserve_bytes=0):
    """Drain stdout incrementally; discard stderr to avoid exposing credentials.

    Capture at most 256 KiB when no streaming output callback is supplied.
    A separate process group lets interrupts/timeouts reap the child and helpers.
    """
    captured = bytearray()
    try:
        proc = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                env=env, start_new_session=True)
    except OSError:
        raise BackupError("Could not start required executable") from None
    deadline = time.monotonic() + timeout
    try:
        with selectors.DefaultSelector() as selector:
            selector.register(proc.stdout, selectors.EVENT_READ)
            while selector.get_map():
                if time.monotonic() >= deadline:
                    raise BackupError("Command exceeded its configured timeout")
                if space_path:
                    check_space(space_path, reserve_bytes)
                for key, _ in selector.select(0.2):
                    chunk = os.read(key.fileobj.fileno(), 65536)
                    if not chunk:
                        selector.unregister(key.fileobj)
                    elif output:
                        output(chunk)
                    else:
                        captured.extend(chunk)
                        if len(captured) > 262144:
                            del captured[:-262144]
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise BackupError("Command exceeded its configured timeout")
            try:
                status = proc.wait(timeout=remaining)
            except subprocess.TimeoutExpired:
                raise BackupError("Command exceeded its configured timeout") from None
        return status, bytes(captured)
    finally:
        if proc.poll() is None:
            os.killpg(proc.pid, signal.SIGTERM)
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(proc.pid, signal.SIGKILL)
                proc.wait()
        proc.stdout.close()
