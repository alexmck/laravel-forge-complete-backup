# Laravel Forge Complete Backup Script

This is a Python script that will backup all files and databases on Laravel Forge servers to an S3 compatible endpoint.

This backup script will work with all Laravel Forge plans, no matter if you have the Business plan or not. Unlike the built in Forge backup, 
this script will backup all your files, and not just the database.

## Features

- Automated backup of Laravel Forge managed servers
- YAML configuration
- Database backup with support for different PHP applications (Laravel, WordPress, Invision Power Board)
- Backup to S3-compatible storage (like Cloudflare R2)
- Discord notifications
- Retention policy management
- Comprehensive logging and error handling

## Installation

1. **Or use the installation script:**
   ```bash
   chmod +x install.sh
   ./install.sh
   ```

## Configuration

The script uses the `config.yaml` file. See the `config.yaml.example` file and copy it as necessary.

## Usage

```bash
source venv/bin/activate
python3 backup.py
```

## Dependencies

- `boto3`: AWS S3 client
- `PyYAML`: YAML configuration parsing
- `requests`: HTTP requests for Discord notifications