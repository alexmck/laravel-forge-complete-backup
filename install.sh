#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")"
umask 077

INSTALL_RESTIC=true
RUN_SETUP=true
for argument in "$@"; do
    case "$argument" in
        --skip-restic) INSTALL_RESTIC=false ;;
        --skip-setup) RUN_SETUP=false ;;
        --help|-h)
            echo 'Usage: bash install.sh [--skip-restic] [--skip-setup]'
            echo 'Default: install dependencies and restic, then run guided setup.'
            echo 'Set PYTHON_BIN to select a separately installed Python 3.10+ interpreter.'
            exit 0 ;;
        *) echo "Unknown option: $argument" >&2; exit 1 ;;
    esac
done
if [[ "$RUN_SETUP" == true && ( ! -t 0 || ! -t 1 ) ]]; then
    echo 'Guided setup requires a terminal. Use --skip-setup for unattended dependency installation.' >&2
    exit 1
fi

BACKUP_PYTHON="${PYTHON_BIN:-python3}"
if ! command -v "$BACKUP_PYTHON" >/dev/null 2>&1; then
    echo 'Python was not found. Install Python 3.10+ or set PYTHON_BIN.' >&2
    exit 1
fi
"$BACKUP_PYTHON" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else "Python 3.10+ is required. Select a newer interpreter with PYTHON_BIN; do not replace system Python.")'
DEPENDENCY_ARGS=()
if [[ "$INSTALL_RESTIC" == false ]]; then
    DEPENDENCY_ARGS+=(--skip-restic)
fi
"$BACKUP_PYTHON" scripts/check_dependencies.py "${DEPENDENCY_ARGS[@]}"
if [[ -x venv/bin/python ]]; then
    venv/bin/python -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else "Existing venv uses an older Python. Move venv aside, then rerun with PYTHON_BIN pointing to Python 3.10+.")'
else
    if ! "$BACKUP_PYTHON" -m venv venv; then
        echo 'Could not create the virtual environment. Install the venv package matching your selected Python (python3-venv for Ubuntu system Python), then rerun.' >&2
        exit 1
    fi
fi
venv/bin/python -m pip install -r requirements.txt

if [[ "$INSTALL_RESTIC" == true ]]; then
    if [[ ! -x bin/restic ]]; then
        "$BACKUP_PYTHON" scripts/install_restic.py
    fi
    bin/restic version
fi
if [[ "$RUN_SETUP" == true ]]; then
    venv/bin/python scripts/setup.py
else
    echo 'Dependencies installed. Run venv/bin/python scripts/setup.py for guided configuration.'
fi
