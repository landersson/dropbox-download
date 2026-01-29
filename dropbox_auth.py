#!/usr/bin/env python3
"""
Set up Dropbox OAuth to get a refresh token for long-running downloads.

This script will:
1. Open a browser for you to authorize the app
2. Get a refresh token that doesn't expire
3. Save credentials to ~/.dropbox_credentials.json

Usage:
    python dropbox_auth.py --app-key YOUR_APP_KEY --app-secret YOUR_APP_SECRET

    Or with just app key (PKCE flow):
    python dropbox_auth.py --app-key YOUR_APP_KEY
"""

import argparse
import json
import os
import sys
import secrets
import hashlib
import base64
from pathlib import Path

import dropbox
from dropbox import DropboxOAuth2FlowNoRedirect


CREDENTIALS_FILE = Path.home() / ".dropbox_credentials.json"


def main():
    parser = argparse.ArgumentParser(
        description="Set up Dropbox OAuth for long-running downloads",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
To get your App Key and App Secret:
1. Go to https://www.dropbox.com/developers/apps
2. Select your app (or create one with 'Scoped access' + 'Full Dropbox')
3. Under 'Permissions' tab, enable: files.content.read, sharing.read
4. Under 'Settings' tab, find 'App key' and 'App secret' (click 'Show')
"""
    )
    parser.add_argument("--app-key", required=True, help="Dropbox App Key")
    parser.add_argument("--app-secret", default=None, help="Dropbox App Secret (optional for PKCE)")
    parser.add_argument("--auth-code", default=None, help="Authorization code (if already obtained)")

    args = parser.parse_args()

    print("Starting Dropbox OAuth flow...")
    print()

    # Use PKCE flow (works with or without app secret)
    if args.app_secret:
        auth_flow = DropboxOAuth2FlowNoRedirect(
            args.app_key,
            consumer_secret=args.app_secret,
            token_access_type='offline',
            use_pkce=True,
        )
    else:
        auth_flow = DropboxOAuth2FlowNoRedirect(
            args.app_key,
            token_access_type='offline',
            use_pkce=True,
        )

    authorize_url = auth_flow.start()

    print("1. Go to this URL in your browser:")
    print()
    print(f"   {authorize_url}")
    print()
    print("2. Click 'Allow' to authorize the app")
    print("3. Copy the authorization code shown")
    print()

    if args.auth_code:
        auth_code = args.auth_code
    else:
        auth_code = input("Enter the authorization code here: ").strip()

    try:
        oauth_result = auth_flow.finish(auth_code)
    except Exception as e:
        print(f"Error: Could not complete authorization: {e}")
        sys.exit(1)

    # Save credentials
    credentials = {
        "app_key": args.app_key,
        "app_secret": args.app_secret,
        "refresh_token": oauth_result.refresh_token,
        "access_token": oauth_result.access_token,
        "expires_at": oauth_result.expires_at.isoformat() if oauth_result.expires_at else None,
    }

    with open(CREDENTIALS_FILE, 'w') as f:
        json.dump(credentials, f, indent=2)

    os.chmod(CREDENTIALS_FILE, 0o600)  # Secure the file

    print()
    print(f"Success! Credentials saved to {CREDENTIALS_FILE}")
    print()

    # Verify it works
    if args.app_secret:
        dbx = dropbox.Dropbox(
            oauth2_refresh_token=oauth_result.refresh_token,
            app_key=args.app_key,
            app_secret=args.app_secret
        )
    else:
        dbx = dropbox.Dropbox(
            oauth2_refresh_token=oauth_result.refresh_token,
            app_key=args.app_key,
        )

    account = dbx.users_get_current_account()
    print(f"Authenticated as: {account.name.display_name}")
    print()
    print("You can now run dropbox_download.py without --token flag.")
    print("The refresh token will automatically renew access tokens as needed.")


if __name__ == "__main__":
    main()
