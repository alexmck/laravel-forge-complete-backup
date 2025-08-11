#!/usr/bin/env python3

"""
Laravel Forge Complete Backup
Author: Alex McKenzie
Version: 0.1
Description: Automated backup solution for Laravel Forge managed servers
"""

import os
import sys
import json
import yaml
import logging
import subprocess
import tempfile
import shutil
import socket
import signal
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Any

# Third-party imports
try:
    import boto3
    from botocore.exceptions import ClientError, NoCredentialsError
except ImportError:
    print("Error: boto3 is required. Install with: pip install boto3")
    sys.exit(1)

try:
    import requests
except ImportError:
    print("Error: requests is required. Install with: pip install requests")
    sys.exit(1)

# Configuration
SCRIPT_DIR = Path(__file__).parent.absolute()
CONFIG_FILE = SCRIPT_DIR / "config.yaml"
BACKUP_DIR = SCRIPT_DIR / "backups"
LOG_FILE = SCRIPT_DIR / "backup.log"
LOCK_FILE = SCRIPT_DIR / "backup.lock"

# ANSI color codes
class Colors:
    RED = '\033[0;31m'
    GREEN = '\033[0;32m'
    YELLOW = '\033[1;33m'
    BLUE = '\033[0;34m'
    NC = '\033[0m'  # No Color

class BackupScript:
    def __init__(self):
        self.config = {}
        self.s3_client = None
        self.discord_webhook_url = ""
        self.s3_config = {}
        self.defaults = {}
        self.sites = []
        self.logger = None
        self.setup_logging()
        
    def setup_logging(self):
        """Setup logging configuration"""
        logging.basicConfig(
            level=logging.INFO,
            format='%(asctime)s [%(levelname)s] %(message)s',
            handlers=[
                logging.FileHandler(LOG_FILE),
                logging.StreamHandler(sys.stdout)
            ]
        )
        self.logger = logging.getLogger(__name__)
        
    def log(self, level: str, message: str):
        """Log message with timestamp"""
        timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        colored_message = f"{timestamp} [{level}] {message}"
        
        # Add color for console output
        if level == "ERROR":
            colored_message = f"{Colors.RED}{colored_message}{Colors.NC}"
        elif level == "SUCCESS":
            colored_message = f"{Colors.GREEN}{colored_message}{Colors.NC}"
        elif level == "WARNING":
            colored_message = f"{Colors.YELLOW}{colored_message}{Colors.NC}"
        elif level == "INFO":
            colored_message = f"{Colors.BLUE}{colored_message}{Colors.NC}"
            
        print(colored_message)
        self.logger.info(message)
        
    def log_error(self, message: str):
        """Log error message"""
        self.log("ERROR", message)
        
    def log_success(self, message: str):
        """Log success message"""
        self.log("SUCCESS", message)
        
    def log_warning(self, message: str):
        """Log warning message"""
        self.log("WARNING", message)
        
    def log_info(self, message: str):
        """Log info message"""
        self.log("INFO", message)
        
    def check_lock(self):
        """Check if script is already running"""
        if LOCK_FILE.exists():
            try:
                pid = int(LOCK_FILE.read_text().strip())
                # Check if process is still running
                os.kill(pid, 0)
                self.log_error(f"Backup script is already running (PID: {pid})")
                sys.exit(1)
            except (ValueError, OSError):
                # Process not running, remove stale lock file
                self.log_warning("Removing stale lock file")
                LOCK_FILE.unlink(missing_ok=True)
                
        # Create lock file
        LOCK_FILE.write_text(str(os.getpid()))
        
    def cleanup_and_exit(self, exit_code: int = 0):
        """Cleanup and exit"""
        LOCK_FILE.unlink(missing_ok=True)
        sys.exit(exit_code)
        
    def load_config(self):
        """Load configuration from YAML file"""
        if not CONFIG_FILE.exists():
            self.log_error(f"Configuration file not found: {CONFIG_FILE}")
            sys.exit(1)
            
        self.log_info(f"Loading configuration from {CONFIG_FILE}")
        
        try:
            with open(CONFIG_FILE, 'r') as f:
                self.config = yaml.safe_load(f)
        except yaml.YAMLError as e:
            self.log_error(f"Error parsing YAML configuration: {e}")
            sys.exit(1)
            
        # Extract global settings
        global_config = self.config.get('global', {})
        self.discord_webhook_url = global_config.get('discord_webhook_url', '')
        
        # S3 configuration
        s3_config = global_config.get('s3', {})
        self.s3_config = {
            'endpoint_url': s3_config.get('endpoint'),
            'bucket': s3_config.get('bucket'),
            'access_key': s3_config.get('access_key'),
            'secret_key': s3_config.get('secret_key'),
            'region': s3_config.get('region', 'auto')
        }
        
        # Validate required S3 settings
        required_s3_fields = ['endpoint_url', 'bucket', 'access_key', 'secret_key']
        missing_fields = [field for field in required_s3_fields if not self.s3_config.get(field)]
        if missing_fields:
            self.log_error(f"Missing required S3 configuration: {', '.join(missing_fields)}")
            sys.exit(1)
            
        # Load defaults
        self.defaults = self.config.get('defaults', {})
        
        # Load sites
        self.sites = self.config.get('sites', [])
        if not self.sites:
            self.log_error("No sites configured in configuration file")
            sys.exit(1)
            
        self.log_success("Configuration loaded successfully")
        
    def setup_s3_client(self):
        """Setup S3 client"""
        try:
            self.s3_client = boto3.client(
                's3',
                endpoint_url=self.s3_config['endpoint_url'],
                aws_access_key_id=self.s3_config['access_key'],
                aws_secret_access_key=self.s3_config['secret_key'],
                region_name=self.s3_config['region']
            )
            self.log_success("S3 client configured successfully")
        except Exception as e:
            self.log_error(f"Failed to setup S3 client: {e}")
            sys.exit(1)
            
    def send_discord_notification(self, title: str, description: str, color: int = 3447003):
        """Send Discord notification"""
        if not self.discord_webhook_url:
            self.log_warning("Discord webhook URL not configured")
            return
            
        try:
            hostname = socket.gethostname()
            timestamp = datetime.utcnow().isoformat() + "Z"
            
            payload = {
                "embeds": [{
                    "title": title,
                    "description": description,
                    "color": color,
                    "timestamp": timestamp,
                    "footer": {
                        "text": f"Server: {hostname}"
                    }
                }]
            }
            
            response = requests.post(
                self.discord_webhook_url,
                json=payload,
                headers={'Content-Type': 'application/json'},
                timeout=10
            )
            response.raise_for_status()
            
        except Exception as e:
            self.log_warning(f"Failed to send Discord notification: {e}")
            
    def extract_db_config(self, site_path: Path) -> Dict[str, str]:
        """Extract database configuration from various config files"""
        db_config = {
            'host': 'localhost',
            'name': '',
            'user': '',
            'password': '',
            'port': '3306'
        }
        
        # Check for .env file (Laravel/general)
        env_file = site_path / '.env'
        if env_file.exists():
            self.log_info("Found .env file, extracting database config")
            try:
                with open(env_file, 'r') as f:
                    for line in f:
                        line = line.strip()
                        if '=' in line and not line.startswith('#'):
                            key, value = line.split('=', 1)
                            key = key.strip()
                            value = value.strip().strip('"\'')
                            
                            if key == 'DB_HOST':
                                db_config['host'] = value
                            elif key == 'DB_DATABASE':
                                db_config['name'] = value
                            elif key == 'DB_USERNAME':
                                db_config['user'] = value
                            elif key == 'DB_PASSWORD':
                                db_config['password'] = value
                            elif key == 'DB_PORT':
                                db_config['port'] = value
            except Exception as e:
                self.log_warning(f"Error reading .env file: {e}")
                
        # Check for wp-config.php (WordPress)
        wp_config = site_path / 'public/wp-config.php'
        if wp_config.exists():
            self.log_info("Found wp-config.php, extracting database config")
            try:
                with open(wp_config, 'r') as f:
                    content = f.read()
                    
                # Extract database configuration using regex
                import re
                
                db_name_match = re.search(r"define\s*\(\s*['\"]DB_NAME['\"],\s*['\"]([^'\"]+)['\"]", content)
                if db_name_match:
                    db_config['name'] = db_name_match.group(1)
                    
                db_user_match = re.search(r"define\s*\(\s*['\"]DB_USER['\"],\s*['\"]([^'\"]+)['\"]", content)
                if db_user_match:
                    db_config['user'] = db_user_match.group(1)
                    
                db_pass_match = re.search(r"define\s*\(\s*['\"]DB_PASSWORD['\"],\s*['\"]([^'\"]+)['\"]", content)
                if db_pass_match:
                    db_config['password'] = db_pass_match.group(1)
                    
                db_host_match = re.search(r"define\s*\(\s*['\"]DB_HOST['\"],\s*['\"]([^'\"]+)['\"]", content)
                if db_host_match:
                    db_config['host'] = db_host_match.group(1)
                    
            except Exception as e:
                self.log_warning(f"Error reading wp-config.php: {e}")
        
        # Check for LocalSettings.php (MediaWiki)
        mw_settings = site_path / 'public/LocalSettings.php'
        if mw_settings.exists():
            self.log_info("Found LocalSettings.php, extracting database config")
            try:
                with open(mw_settings, 'r') as f:
                    content = f.read()

                import re

                server_match = re.search(r"\$wgDBserver\s*=\s*['\"]([^'\"]+)['\"]", content)
                if server_match:
                    server_value = server_match.group(1).strip()
                    # If server is in host:port format, split accordingly (avoid IPv6 and socket paths)
                    if server_value.count(':') == 1 and '/' not in server_value:
                        host_part, port_part = server_value.rsplit(':', 1)
                        if host_part:
                            db_config['host'] = host_part
                        if port_part.isdigit():
                            db_config['port'] = port_part
                    else:
                        db_config['host'] = server_value

                name_match = re.search(r"\$wgDBname\s*=\s*['\"]([^'\"]+)['\"]", content)
                if name_match:
                    db_config['name'] = name_match.group(1)

                user_match = re.search(r"\$wgDBuser\s*=\s*['\"]([^'\"]+)['\"]", content)
                if user_match:
                    db_config['user'] = user_match.group(1)

                pass_match = re.search(r"\$wgDBpassword\s*=\s*['\"]([^'\"]*)['\"]", content)
                if pass_match:
                    db_config['password'] = pass_match.group(1)

            except Exception as e:
                self.log_warning(f"Error reading LocalSettings.php: {e}")
                
        # Check for conf_global.php (Invision Power Board)
        conf_global = site_path / 'public/conf_global.php'
        if conf_global.exists():
            self.log_info("Found conf_global.php, extracting database config")
            try:
                with open(conf_global, 'r') as f:
                    content = f.read()
                    
                import re
                
                # Support both assignment syntax ($INFO['key'] = 'value') and array syntax ($INFO = array('key' => 'value'))
                def first_match(patterns):
                    for pattern in patterns:
                        match = re.search(pattern, content)
                        if match:
                            return match.group(1)
                    return None

                host_value = first_match([
                    r"\$INFO\s*\[\s*['\"]sql_host['\"]\s*\]\s*=\s*['\"]([^'\"]+)['\"]",
                    r"['\"]sql_host['\"]\s*=>\s*['\"]([^'\"]+)['\"]",
                ])
                if host_value:
                    # Handle optional host:port format (avoid IPv6 and socket paths)
                    if host_value.count(':') == 1 and '/' not in host_value:
                        host_part, port_part = host_value.rsplit(':', 1)
                        if host_part:
                            db_config['host'] = host_part
                        if port_part.isdigit():
                            db_config['port'] = port_part
                    else:
                        db_config['host'] = host_value

                db_value = first_match([
                    r"\$INFO\s*\[\s*['\"]sql_database['\"]\s*\]\s*=\s*['\"]([^'\"]+)['\"]",
                    r"['\"]sql_database['\"]\s*=>\s*['\"]([^'\"]+)['\"]",
                ])
                if db_value:
                    db_config['name'] = db_value

                user_value = first_match([
                    r"\$INFO\s*\[\s*['\"]sql_user['\"]\s*\]\s*=\s*['\"]([^'\"]+)['\"]",
                    r"['\"]sql_user['\"]\s*=>\s*['\"]([^'\"]+)['\"]",
                ])
                if user_value:
                    db_config['user'] = user_value

                pass_value = first_match([
                    r"\$INFO\s*\[\s*['\"]sql_pass['\"]\s*\]\s*=\s*['\"]([^'\"]*)['\"]",
                    r"['\"]sql_pass['\"]\s*=>\s*['\"]([^'\"]*)['\"]",
                ])
                if pass_value is not None:
                    db_config['password'] = pass_value

                # Optional explicit port key
                port_value = first_match([
                    r"\$INFO\s*\[\s*['\"]sql_port['\"]\s*\]\s*=\s*['\"](\d+)['\"]",
                    r"['\"]sql_port['\"]\s*=>\s*['\"](\d+)['\"]",
                ])
                if port_value and port_value.isdigit():
                    db_config['port'] = port_value
                    
            except Exception as e:
                self.log_warning(f"Error reading conf_global.php: {e}")
                
        return db_config
        
    def backup_database(self, site_name: str, site_path: Path, backup_path: Path) -> bool:
        """Create database backup"""
        self.log_info(f"Starting database backup for {site_name}")
        
        # Check if mysqldump is available
        if not shutil.which('mysqldump'):
            self.log_warning("mysqldump not available, skipping database backup")
            return False
            
        db_config = self.extract_db_config(site_path)
        
        if not db_config['name'] or not db_config['user']:
            self.log_warning(f"Database configuration not found for {site_name}, skipping database backup")
            return False
            
        db_backup_file = backup_path / f"{site_name}_database.sql"
        
        try:
            # Use mysqldump command for better compatibility
            cmd = [
                'mysqldump',
                '-h', db_config['host'],
                '-P', db_config['port'],
                '-u', db_config['user']
            ]
            
            if db_config['password']:
                cmd.extend(['-p' + db_config['password']])
                
            cmd.extend([
                '--single-transaction',
                '--routines',
                '--triggers',
                db_config['name']
            ])
            
            with open(db_backup_file, 'w') as f:
                result = subprocess.run(cmd, stdout=f, stderr=subprocess.PIPE, text=True)
                
            if result.returncode == 0:
                self.log_success(f"Database backup created: {db_backup_file}")
                return True
            else:
                self.log_warning(f"Failed to backup database for {site_name}: {result.stderr}")
                db_backup_file.unlink(missing_ok=True)
                return False
                
        except Exception as e:
            self.log_warning(f"Failed to backup database for {site_name}: {e}")
            db_backup_file.unlink(missing_ok=True)
            return False
            
    def create_backup_archive(self, site_name: str, site_path: Path, temp_dir: Path, 
                             exclude_patterns: List[str], compression_level: int) -> Path:
        """Create backup archive"""
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        backup_filename = f"{site_name}_{timestamp}.tar.gz"
        local_backup_path = BACKUP_DIR / backup_filename
        
        # Create exclude file if patterns are specified
        exclude_file = None
        if exclude_patterns:
            exclude_file = temp_dir / "exclude_patterns.txt"
            with open(exclude_file, 'w') as f:
                for pattern in exclude_patterns:
                    f.write(f"{pattern}\n")
                    
        # Create tar command
        cmd = ['tar', '-czf', str(local_backup_path)]
        
        if exclude_file:
            cmd.extend(['--exclude-from', str(exclude_file)])
            
        # Add site directory
        cmd.extend(['-C', str(site_path.parent), site_path.name])
        
        # Add temp directory contents
        if temp_dir.exists() and any(temp_dir.iterdir()):
            cmd.extend(['-C', str(temp_dir), '.'])
            
        self.log_info(f"Creating archive: {backup_filename}")
        
        try:
            result = subprocess.run(cmd, check=True, capture_output=True, text=True)
            return local_backup_path
        except subprocess.CalledProcessError as e:
            self.log_error(f"Failed to create archive: {e}")
            raise
            
    def upload_to_s3(self, local_path: Path, s3_key: str) -> bool:
        """Upload file to S3"""
        try:
            self.s3_client.upload_file(str(local_path), self.s3_config['bucket'], s3_key)
            return True
        except Exception as e:
            self.log_error(f"Failed to upload to S3: {e}")
            return False
            
    def cleanup_old_backups(self, site_name: str, retention_days: int):
        """Clean up old backups"""
        self.log_info(f"Cleaning up old backups for {site_name} (keeping {retention_days} days)")
        
        try:
            # List all backups for the site
            response = self.s3_client.list_objects_v2(
                Bucket=self.s3_config['bucket'],
                Prefix=f"{site_name}/"
            )
            
            if 'Contents' not in response:
                return
                
            # Filter and sort backups
            backups = []
            for obj in response['Contents']:
                key = obj['Key']
                if key.startswith(f"{site_name}/{site_name}_") and key.endswith('.tar.gz'):
                    backups.append({
                        'key': key,
                        'last_modified': obj['LastModified']
                    })
                    
            # Sort by date (oldest first)
            backups.sort(key=lambda x: x['last_modified'])
            
            # Calculate how many to keep
            backups_to_keep = retention_days
            if len(backups) > backups_to_keep:
                backups_to_delete = len(backups) - backups_to_keep
                deleted_count = 0
                
                for backup in backups[:backups_to_delete]:
                    try:
                        self.s3_client.delete_object(
                            Bucket=self.s3_config['bucket'],
                            Key=backup['key']
                        )
                        filename = backup['key'].split('/')[-1]
                        self.log_success(f"Deleted old backup: {filename}")
                        self.send_discord_notification(
                            "🗑️ **Old Backup Deleted**",
                            f"Site: {site_name}\nFile: {filename}",
                            10181046
                        )
                        deleted_count += 1
                    except Exception as e:
                        filename = backup['key'].split('/')[-1]
                        self.log_warning(f"Failed to delete old backup {filename}: {e}")
                        
        except Exception as e:
            self.log_warning(f"Error during cleanup for {site_name}: {e}")
            
    def backup_site(self, site_config: Dict[str, Any]) -> bool:
        """Backup a single site"""
        site_name = site_config['name']
        user_path = Path(site_config['user_path'])
        retention_days = site_config.get('retention_days', self.defaults.get('retention_days', 7))
        backup_db = site_config.get('backup_database', self.defaults.get('backup_database', True))
        compression_level = site_config.get('compression_level', self.defaults.get('compression_level', 6))
        exclude_patterns = site_config.get('exclude_patterns', [])
        
        self.log_info(f"Starting backup for site: {site_name}")
        self.log_info(f"Path: {user_path}, Retention: {retention_days} days, DB: {backup_db}")
        
        # Check if site path exists
        if not user_path.exists():
            self.log_warning(f"Site path does not exist: {user_path}, skipping {site_name}")
            self.send_discord_notification(
                "⚠️ **Backup Warning**",
                f"Site path not found: {user_path} for {site_name}",
                16776960
            )
            return False
            
        # Create temporary backup directory
        temp_dir = Path(tempfile.mkdtemp(prefix=f"backup_{site_name}_"))
        
        try:
            # Backup database if enabled
            if backup_db:
                self.backup_database(site_name, user_path, temp_dir)
                
            # Create backup archive
            local_backup_path = self.create_backup_archive(
                site_name, user_path, temp_dir, exclude_patterns, compression_level
            )
            
            # Get backup size
            backup_size = local_backup_path.stat().st_size
            backup_size_mb = backup_size / (1024 * 1024)
            
            self.log_success(f"Archive created successfully: {local_backup_path.name} ({backup_size_mb:.1f} MB)")
            
            # Upload to S3
            s3_key = f"{site_name}/{local_backup_path.name}"
            self.log_info(f"Uploading to S3: {local_backup_path.name}")
            
            if self.upload_to_s3(local_backup_path, s3_key):
                self.log_success(f"Upload completed: {local_backup_path.name}")
                self.send_discord_notification(
                    "✅ **Backup Successful**",
                    f"Site: {site_name}\nSize: {backup_size_mb:.1f} MB\nFile: {local_backup_path.name}",
                    3066993
                )
                
                # Clean up old backups
                self.cleanup_old_backups(site_name, retention_days)
                
                # Remove local backup
                local_backup_path.unlink()
                self.log_info(f"Local backup removed: {local_backup_path.name}")
                return True
            else:
                self.log_error(f"Failed to upload backup: {local_backup_path.name}")
                return False
                
        except Exception as e:
            self.log_error(f"Failed to backup site {site_name}: {e}")
            return False
        finally:
            # Clean up temporary directory
            shutil.rmtree(temp_dir, ignore_errors=True)
            
    def main(self):
        """Main backup process"""
        self.log_info("Starting backup process")
        
        # Check for lock file
        self.check_lock()
        
        # Create backup directory
        BACKUP_DIR.mkdir(exist_ok=True)
        
        # Load configuration
        self.load_config()
        
        # Setup S3 client
        self.setup_s3_client()
        
        # Process each site
        site_count = len(self.sites)
        successful_backups = 0
        failed_backups = 0
        
        for site_config in self.sites:
            if self.backup_site(site_config):
                successful_backups += 1
            else:
                failed_backups += 1
                
        # Send summary notification
        summary = f"**Backup Summary**\n"
        summary += f"Sites processed: {site_count}\n"
        summary += f"Successful: {successful_backups}\n"
        summary += f"Failed: {failed_backups}"
        
        if failed_backups == 0:
            self.send_discord_notification("📊 **Backup Complete**", summary, 3066993)
            self.log_success("Backup process completed successfully")
        else:
            self.send_discord_notification("⚠️ **Backup Complete with Errors**", summary, 16776960)
            self.log_warning(f"Backup process completed with {failed_backups} failures")
            
        self.cleanup_and_exit(0)
        
def signal_handler(signum, frame):
    """Handle interrupt signals"""
    print("\nReceived interrupt signal, cleaning up...")
    LOCK_FILE.unlink(missing_ok=True)
    sys.exit(130)
    
if __name__ == "__main__":
    # Setup signal handlers
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    
    # Create and run backup script
    backup_script = BackupScript()
    backup_script.main() 