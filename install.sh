#!/bin/bash

# Installation script for Python backup script

echo "Installing Python backup script dependencies..."

# Check current Python version
CURRENT_PYTHON_VERSION=$(python3 --version 2>&1 | grep -oE '[0-9]+\.[0-9]+')
echo "Current Python version: $CURRENT_PYTHON_VERSION"

# Determine if we need to upgrade Python
NEED_UPGRADE=false
REQUIRED_VERSION="3.8"
if [ "$(printf '%s\n' "$REQUIRED_VERSION" "$CURRENT_PYTHON_VERSION" | sort -V | head -n1)" != "$REQUIRED_VERSION" ]; then
    echo "Python version < 3.8 detected. Recommending pyenv for Python management..."
    NEED_UPGRADE=true
else
    echo "Python version is already 3.8+ (compatible with all features)."
    PYTHON_CMD="python3"
fi

# Guide user to install pyenv if needed
if [ "$NEED_UPGRADE" = true ]; then
    echo ""
    echo "================================================"
    echo "Python 3.8+ is required for this script."
    echo "We recommend using pyenv to manage Python versions."
    echo ""
    echo "To install pyenv and a modern Python version:"
    echo ""
    echo "1. Install pyenv:"
    echo "   curl https://pyenv.run | bash"
    echo ""
    echo "2. Add to your shell profile (~/.bashrc, ~/.zshrc, etc.):"
    echo "   export PYENV_ROOT=\"\$HOME/.pyenv\""
    echo "   export PATH=\"\$PYENV_ROOT/bin:\$PATH\""
    echo "   eval \"\$(pyenv init -)\""
    echo ""
    echo "3. Restart your shell or run: source ~/.bashrc"
    echo ""
    echo "4. Install Python 3.8+:"
    echo "   pyenv install 3.11.0  # or any 3.8+ version"
    echo "   pyenv global 3.11.0"
    echo ""
    echo "5. Run this install script again"
    echo "================================================"
    echo ""
    exit 1
fi

# Check if pip is available
if ! $PYTHON_CMD -m pip --version &> /dev/null; then
    echo "Error: pip is not installed or not available."
    echo ""
    echo "================================================"
    echo "pip is required for this script to work."
    echo "Please install pip manually using one of these methods:"
    echo ""
    echo "For Ubuntu/Debian:"
    echo "  sudo apt-get update && sudo apt-get install -y python3-pip"
    echo ""
    echo "Or using ensurepip:"
    echo "  sudo $PYTHON_CMD -m ensurepip --upgrade"
    echo ""
    echo "After installing pip, run this install script again."
    echo "================================================"
    echo ""
    exit 1
fi

# Check if venv module is available
if ! $PYTHON_CMD -c "import venv" &> /dev/null; then
    echo "Error: venv module is not available."
    echo "Please ensure you have a Python installation with the venv module."
    exit 1
fi

# Remove existing venv if it exists
if [ -d "venv" ]; then
    echo "Removing existing virtual environment..."
    rm -rf venv
fi

# Create virtual environment
echo "Creating virtual environment with $PYTHON_CMD..."
$PYTHON_CMD -m venv venv
if [ $? -ne 0 ]; then
    echo "Error: Failed to create virtual environment."
    exit 1
fi

# Activate virtual environment
source venv/bin/activate

# Upgrade pip in the virtual environment
echo "Upgrading pip in virtual environment..."
pip install --upgrade pip

# Install dependencies
echo "Installing Python dependencies..."
pip install -r requirements.txt

# Make backup script executable
chmod +x backup.py

echo ""
echo "Installation complete!"
echo "Python version in virtual environment: $(python --version)"
echo ""
echo "To run the backup script:"
echo "  source venv/bin/activate"
echo "  python backup.py"
echo ""