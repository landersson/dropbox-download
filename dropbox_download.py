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

import argparse
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

import dropbox
from dropbox.exceptions import ApiError, AuthError
from dropbox.files import FolderMetadata, FileMetadata
import requests.exceptions


MAX_RETRIES = 5
RETRY_DELAY = 5  # seconds
CREDENTIALS_FILE = Path.home() / ".dropbox_credentials.json"


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

            # Verify size
            if local_path.stat().st_size == expected_size:
                return True
            else:
                print(f"\n  Size mismatch for {file_path}, retrying...")
                local_path.unlink()
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
                              dry_run: bool = False) -> tuple[int, int, int]:
    """Recursively download all files from a folder.

    Returns: (success_count, fail_count, skipped_count)
    """
    success_count = 0
    fail_count = 0
    skipped_count = 0

    entries = list_folder_contents(dbx, shared_link_url, remote_path)

    for entry in entries:
        # Build the path relative to the shared link
        if remote_path:
            entry_path = f"{remote_path}/{entry.name}"
        else:
            entry_path = f"/{entry.name}"

        if isinstance(entry, FolderMetadata):
            # Recurse into subfolder
            s, f, sk = download_folder_recursive(
                dbx, shared_link_url,
                entry_path,
                local_base,
                dry_run
            )
            success_count += s
            fail_count += f
            skipped_count += sk

        elif isinstance(entry, FileMetadata):
            # Build local path
            relative_path = entry_path.lstrip('/')
            local_path = local_base / relative_path

            if dry_run:
                size_mb = entry.size / (1024 * 1024)
                print(f"  Would download: {entry_path} ({size_mb:.2f} MB) -> {local_path}")
                success_count += 1
            else:
                # Check if already exists
                if local_path.exists() and local_path.stat().st_size == entry.size:
                    skipped_count += 1
                    continue

                size_mb = entry.size / (1024 * 1024)
                print(f"  Downloading: {entry_path} ({size_mb:.2f} MB)...", end=" ", flush=True)
                if download_file(dbx, shared_link_url, entry_path, local_path, entry.size):
                    print("OK")
                    success_count += 1
                else:
                    print("FAILED")
                    fail_count += 1

    return success_count, fail_count, skipped_count


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
    print(f"\n{'Listing' if args.dry_run else 'Downloading'} files (skipping already downloaded)...")

    # Download files (start with empty path for root of shared folder)
    success, fail, skipped = download_folder_recursive(
        dbx, args.url, "", output_dir, args.dry_run
    )

    print(f"\nComplete:")
    print(f"  Downloaded: {success} files")
    print(f"  Skipped (already existed): {skipped} files")
    if fail > 0:
        print(f"  Failed: {fail} files")
        sys.exit(1)


if __name__ == "__main__":
    main()
