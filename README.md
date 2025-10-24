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

1. SSH into your Forge server as the `forge` user.

2. ```bash
   cd /home/forge && git clone https://github.com/alexmck/laravel-forge-complete-backup.git && cd laravel-forge-complete-backup
   ```

1. **Run the installation script:**
   ```bash
   chmod +x install.sh
   ./install.sh
   ```

   To run this automatically at 3:00 AM every day via cron, add an entry like this (update the absolute paths to match your system). You can either use `crontab -e` via SSH to add this, or you can add it as a scheduled job via the Laravel Forge web interface.

   ```bash
   0 3 * * * /home/forge/laravel-forge-complete-backup/venv/bin/python /home/forge/laravel-forge-complete-backup/backup.py >> /home/forge/laravel-forge-complete-backup/cron.log 2>&1
   ```

## Configuration

The script uses the `config.yaml` file. See the `config.yaml.example` file and copy it as necessary.

## Manually Running a Backup

```bash
source venv/bin/activate
python3 backup.py
```

## Updating Backup Script

```bash
cd /home/forge/laravel-forge-complete-backup
git pull origin main
```

## Dependencies

- `boto3`: AWS S3 client
- `PyYAML`: YAML configuration parsing
- `requests`: HTTP requests for Discord notifications