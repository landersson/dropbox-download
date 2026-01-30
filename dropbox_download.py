#!/usr/bin/env python3
"""
Download files from a Dropbox shared folder link with resume support and auto-refresh.

Usage:
    Option 1: Use OAuth credentials (recommended for large downloads)
        python dropbox_auth.py --app-key KEY --app-secret SECRET  # One-time setup
        python dropbox_download.py "https://www.dropbox.com/scl/fo/..."

    Option 2: Use short-lived access token
        python dropbox_download.py --token TOKEN "https://www.dropbox.com/scl/fo/..."
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import dropbox
from dropbox.exceptions import ApiError, AuthError
from dropbox.files import FolderMetadata, FileMetadata
import requests.exceptions


MAX_RETRIES = 5
RETRY_DELAY = 5  # seconds
CREDENTIALS_FILE = Path.home() / ".dropbox_credentials.json"


@dataclass
class DownloadStats:
    """Track download progress and calculate speed/ETA."""
    total_files: int = 0
    total_bytes: int = 0
    downloaded_files: int = 0
    downloaded_bytes: int = 0
    skipped_files: int = 0
    skipped_bytes: int = 0
    failed_files: int = 0
    start_time: float = field(default_factory=time.time)

    # For rolling average speed calculation
    recent_downloads: list = field(default_factory=list)  # [(timestamp, bytes), ...]

    def add_download(self, size: int):
        """Record a completed download."""
        self.downloaded_files += 1
        self.downloaded_bytes += size
        self.recent_downloads.append((time.time(), size))
        # Keep only last 20 downloads for rolling average
        if len(self.recent_downloads) > 20:
            self.recent_downloads.pop(0)

    def add_skip(self, size: int):
        """Record a skipped file."""
        self.skipped_files += 1
        self.skipped_bytes += size

    def add_failure(self):
        """Record a failed download."""
        self.failed_files += 1

    def get_speed_mbps(self) -> float:
        """Calculate current download speed in MB/s using rolling average."""
        if len(self.recent_downloads) < 2:
            # Use overall average if not enough recent data
            elapsed = time.time() - self.start_time
            if elapsed > 0:
                return (self.downloaded_bytes / (1024 * 1024)) / elapsed
            return 0.0

        # Calculate speed from recent downloads
        first_time = self.recent_downloads[0][0]
        last_time = self.recent_downloads[-1][0]
        total_bytes = sum(size for _, size in self.recent_downloads)

        elapsed = last_time - first_time
        if elapsed > 0:
            return (total_bytes / (1024 * 1024)) / elapsed
        return 0.0

    def get_eta_str(self) -> str:
        """Calculate estimated time remaining."""
        speed_bps = self.get_speed_mbps() * 1024 * 1024  # Convert to bytes/sec
        if speed_bps <= 0:
            return "calculating..."

        remaining_bytes = self.total_bytes - self.downloaded_bytes - self.skipped_bytes
        if remaining_bytes <= 0:
            return "done"

        seconds_remaining = remaining_bytes / speed_bps

        if seconds_remaining < 60:
            return f"{int(seconds_remaining)}s"
        elif seconds_remaining < 3600:
            mins = int(seconds_remaining / 60)
            secs = int(seconds_remaining % 60)
            return f"{mins}m {secs}s"
        else:
            hours = int(seconds_remaining / 3600)
            mins = int((seconds_remaining % 3600) / 60)
            return f"{hours}h {mins}m"

    def get_progress_str(self) -> str:
        """Get a progress string with speed and ETA."""
        speed = self.get_speed_mbps()
        eta = self.get_eta_str()
        pct = 0
        if self.total_bytes > 0:
            pct = ((self.downloaded_bytes + self.skipped_bytes) / self.total_bytes) * 100

        downloaded_gb = self.downloaded_bytes / (1024 * 1024 * 1024)
        total_gb = self.total_bytes / (1024 * 1024 * 1024)

        return f"[{pct:.1f}% | {downloaded_gb:.2f}/{total_gb:.2f} GB | {speed:.2f} MB/s | ETA: {eta}]"


def load_credentials() -> dict | None:
    """Load saved OAuth credentials."""
    if CREDENTIALS_FILE.exists():
        with open(CREDENTIALS_FILE) as f:
            return json.load(f)
    return None


def save_credentials(credentials: dict):
    """Save updated credentials (after token refresh)."""
    with open(CREDENTIALS_FILE, 'w') as f:
        json.dump(credentials, f, indent=2)
    os.chmod(CREDENTIALS_FILE, 0o600)


def create_dropbox_client(token: str = None) -> dropbox.Dropbox:
    """Create Dropbox client, preferring OAuth credentials with refresh token."""
    credentials = load_credentials()

    if credentials and credentials.get('refresh_token'):
        # Use OAuth with refresh token (auto-refreshes when expired)
        dbx = dropbox.Dropbox(
            oauth2_refresh_token=credentials['refresh_token'],
            app_key=credentials['app_key'],
            app_secret=credentials['app_secret']
        )
        return dbx

    elif token:
        # Use provided access token (will expire)
        return dropbox.Dropbox(token)

    else:
        return None


def list_folder_contents(dbx: dropbox.Dropbox, shared_link_url: str, path: str = "") -> list:
    """List all files and folders in a shared folder."""
    all_entries = []

    shared_link = dropbox.files.SharedLink(url=shared_link_url)

    for attempt in range(MAX_RETRIES):
        try:
            result = dbx.files_list_folder(
                path=path,
                shared_link=shared_link
            )
            all_entries.extend(result.entries)

            while result.has_more:
                result = dbx.files_list_folder_continue(result.cursor)
                all_entries.extend(result.entries)

            return all_entries

        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as e:
            if attempt < MAX_RETRIES - 1:
                delay = RETRY_DELAY * (attempt + 1)
                print(f"\n  Connection error listing folder, retrying in {delay}s... ({attempt + 1}/{MAX_RETRIES})")
                time.sleep(delay)
            else:
                raise
        except ApiError as e:
            print(f"Error listing folder '{path}': {e}")
            raise

    return all_entries


def scan_folder_recursive(dbx: dropbox.Dropbox, shared_link_url: str,
                          remote_path: str, local_base: Path) -> tuple[int, int, int, int]:
    """Scan folder to count files and bytes (for progress calculation).

    Returns: (total_files, total_bytes, skip_files, skip_bytes)
    """
    total_files = 0
    total_bytes = 0
    skip_files = 0
    skip_bytes = 0

    entries = list_folder_contents(dbx, shared_link_url, remote_path)

    for entry in entries:
        if remote_path:
            entry_path = f"{remote_path}/{entry.name}"
        else:
            entry_path = f"/{entry.name}"

        if isinstance(entry, FolderMetadata):
            tf, tb, sf, sb = scan_folder_recursive(dbx, shared_link_url, entry_path, local_base)
            total_files += tf
            total_bytes += tb
            skip_files += sf
            skip_bytes += sb

        elif isinstance(entry, FileMetadata):
            total_files += 1
            total_bytes += entry.size

            # Check if already exists
            relative_path = entry_path.lstrip('/')
            local_path = (local_base / relative_path).resolve()
            if local_path.exists() and local_path.stat().st_size == entry.size:
                skip_files += 1
                skip_bytes += entry.size

    return total_files, total_bytes, skip_files, skip_bytes


def download_file(dbx: dropbox.Dropbox, shared_link_url: str, file_path: str,
                  local_path: Path, expected_size: int) -> bool:
    """Download a single file from the shared folder with retry logic."""

    # Check if file already exists with correct size
    if local_path.exists():
        existing_size = local_path.stat().st_size
        if existing_size == expected_size:
            return True  # Already downloaded
        else:
            # Partial download, remove and retry
            local_path.unlink()

    for attempt in range(MAX_RETRIES):
        try:
            # Create parent directories if needed
            local_path.parent.mkdir(parents=True, exist_ok=True)

            # Download the file using sharing_get_shared_link_file
            metadata, response = dbx.sharing_get_shared_link_file(
                url=shared_link_url,
                path=file_path if file_path else None
            )

            with open(local_path, 'wb') as f:
                f.write(response.content)
                f.flush()
                os.fsync(f.fileno())

            # Verify size (with retry for slow filesystems)
            for _ in range(3):
                try:
                    if local_path.exists() and local_path.stat().st_size == expected_size:
                        return True
                    time.sleep(0.1)
                except OSError:
                    time.sleep(0.1)

            # Size mismatch or file missing
            print(f"\n  Size mismatch or missing file for {file_path}, retrying...")
            try:
                local_path.unlink()
            except FileNotFoundError:
                pass
            continue

        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as e:
            if attempt < MAX_RETRIES - 1:
                delay = RETRY_DELAY * (attempt + 1)
                print(f"\n  Connection error, retrying in {delay}s... ({attempt + 1}/{MAX_RETRIES})")
                time.sleep(delay)
            else:
                print(f"\n  Failed after {MAX_RETRIES} attempts: {e}")
                return False

        except AuthError as e:
            # Token might have expired even with refresh token in rare cases
            print(f"\n  Auth error: {e}")
            return False

        except ApiError as e:
            if attempt < MAX_RETRIES - 1:
                delay = RETRY_DELAY * (attempt + 1)
                print(f"\n  API error, retrying in {delay}s... ({attempt + 1}/{MAX_RETRIES}): {e}")
                time.sleep(delay)
            else:
                print(f"\n  Failed after {MAX_RETRIES} attempts: {e}")
                return False

    return False


def download_folder_recursive(dbx: dropbox.Dropbox, shared_link_url: str,
                              remote_path: str, local_base: Path,
                              stats: DownloadStats,
                              dry_run: bool = False) -> None:
    """Recursively download all files from a folder."""

    entries = list_folder_contents(dbx, shared_link_url, remote_path)

    for entry in entries:
        # Build the path relative to the shared link
        if remote_path:
            entry_path = f"{remote_path}/{entry.name}"
        else:
            entry_path = f"/{entry.name}"

        if isinstance(entry, FolderMetadata):
            # Recurse into subfolder
            download_folder_recursive(
                dbx, shared_link_url,
                entry_path,
                local_base,
                stats,
                dry_run
            )

        elif isinstance(entry, FileMetadata):
            # Build local path - normalize to avoid double slashes
            relative_path = entry_path.lstrip('/')
            local_path = (local_base / relative_path).resolve()

            # Check if already exists with correct size
            if local_path.exists() and local_path.stat().st_size == entry.size:
                stats.add_skip(entry.size)
                continue

            size_mb = entry.size / (1024 * 1024)

            if dry_run:
                print(f"  Would download: {entry_path} ({size_mb:.2f} MB) -> {local_path}")
                stats.downloaded_files += 1
            else:
                progress = stats.get_progress_str()
                print(f"  {progress} {entry.name} ({size_mb:.2f} MB)...", end=" ", flush=True)
                if download_file(dbx, shared_link_url, entry_path, local_path, entry.size):
                    print("OK")
                    stats.add_download(entry.size)
                else:
                    print("FAILED")
                    stats.add_failure()


def main():
    parser = argparse.ArgumentParser(
        description="Download files from a Dropbox shared folder",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__
    )
    parser.add_argument("url", help="Dropbox shared folder URL (use the Share URL from Dropbox)")
    parser.add_argument("-o", "--output", default=".",
                        help="Output directory (default: current directory)")
    parser.add_argument("--dry-run", action="store_true",
                        help="List files without downloading")
    parser.add_argument("--token",
                        help="Dropbox access token (or use dropbox_auth.py for OAuth)")

    args = parser.parse_args()

    # Create Dropbox client
    token = args.token or os.environ.get('DROPBOX_ACCESS_TOKEN')
    dbx = create_dropbox_client(token)

    if not dbx:
        print("Error: No credentials found.")
        print()
        print("Option 1: Set up OAuth (recommended for large downloads):")
        print("  python dropbox_auth.py --app-key KEY --app-secret SECRET")
        print()
        print("Option 2: Use a short-lived access token:")
        print("  python dropbox_download.py --token TOKEN URL")
        print()
        print("To get app key/secret or access token:")
        print("  Go to https://www.dropbox.com/developers/apps")
        sys.exit(1)

    # Verify authentication works
    try:
        account = dbx.users_get_current_account()
        print(f"Authenticated as: {account.name.display_name}")
    except AuthError as e:
        print(f"Authentication error: {e}")
        print("Try running dropbox_auth.py again to refresh credentials.")
        sys.exit(1)
    except Exception as e:
        print(f"Error authenticating: {e}")
        sys.exit(1)

    # Get info about the shared link
    print(f"\nShared link: {args.url}")
    try:
        metadata = dbx.sharing_get_shared_link_metadata(url=args.url)
        print(f"Folder name: {metadata.name}")
    except ApiError as e:
        print(f"Error getting shared link info: {e}")
        sys.exit(1)

    # Set up output directory
    output_dir = Path(args.output)
    output_dir.mkdir(parents=True, exist_ok=True)
    print(f"Output directory: {output_dir.absolute()}")

    # Scan folder first to get totals
    print("\nScanning folder contents...")
    total_files, total_bytes, skip_files, skip_bytes = scan_folder_recursive(
        dbx, args.url, "", output_dir
    )

    total_gb = total_bytes / (1024 * 1024 * 1024)
    skip_gb = skip_bytes / (1024 * 1024 * 1024)
    to_download = total_files - skip_files
    to_download_gb = (total_bytes - skip_bytes) / (1024 * 1024 * 1024)

    print(f"  Total: {total_files} files ({total_gb:.2f} GB)")
    print(f"  Already downloaded: {skip_files} files ({skip_gb:.2f} GB)")
    print(f"  To download: {to_download} files ({to_download_gb:.2f} GB)")

    if to_download == 0:
        print("\nAll files already downloaded!")
        sys.exit(0)

    # Initialize stats
    stats = DownloadStats(
        total_files=total_files,
        total_bytes=total_bytes,
        skipped_files=skip_files,
        skipped_bytes=skip_bytes,
    )

    print(f"\n{'Listing' if args.dry_run else 'Downloading'} files...")

    # Download files (start with empty path for root of shared folder)
    download_folder_recursive(dbx, args.url, "", output_dir, stats, args.dry_run)

    # Final summary
    elapsed = time.time() - stats.start_time
    elapsed_str = f"{int(elapsed // 3600)}h {int((elapsed % 3600) // 60)}m {int(elapsed % 60)}s"
    avg_speed = (stats.downloaded_bytes / (1024 * 1024)) / elapsed if elapsed > 0 else 0

    print(f"\nComplete:")
    print(f"  Downloaded: {stats.downloaded_files} files ({stats.downloaded_bytes / (1024**3):.2f} GB)")
    print(f"  Skipped (already existed): {stats.skipped_files} files")
    print(f"  Time: {elapsed_str}")
    print(f"  Average speed: {avg_speed:.2f} MB/s")

    if stats.failed_files > 0:
        print(f"  Failed: {stats.failed_files} files")
        sys.exit(1)


if __name__ == "__main__":
    main()
