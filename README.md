# Dropbox Folder Downloader

Download large folders from Dropbox shared links that are too big for the web interface.

## Features

- Downloads files from Dropbox shared folder links
- **Resume support**: Skips already-downloaded files
- **Auto-refresh**: OAuth refresh tokens automatically renew (no 4-hour expiry)
- **Retry logic**: Handles connection drops with exponential backoff
- **Recursive**: Downloads all subfolders

## Prerequisites

- Python 3.7 or later
- `dropbox` Python package

```bash
# Create and activate a virtual environment
python3 -m venv ~/.virtualenvs/dropbox
source ~/.virtualenvs/dropbox/bin/activate

# Install dependencies
pip install dropbox
```

## Setup

### 1. Create a Dropbox App

1. Go to https://www.dropbox.com/developers/apps
2. Click **"Create app"**
3. Select **"Scoped access"**
4. Select **"Full Dropbox"**
5. Give it a name (e.g., "folder-downloader")
6. Click **"Create app"**

### 2. Configure Permissions

1. Go to the **Permissions** tab
2. Enable these checkboxes:
   - `files.content.read`
   - `sharing.read`
3. Click **"Submit"** at the bottom to save

### 3. Get App Credentials

1. Go to the **Settings** tab
2. Copy the **App key**
3. Click **"Show"** next to App secret and copy it

### 4. Authenticate

Run the authentication script:

```bash
python dropbox_auth.py --app-key YOUR_APP_KEY --app-secret YOUR_APP_SECRET
```

This will:
1. Display a URL to visit in your browser
2. Ask you to authorize the app and copy the code
3. Save credentials to `~/.dropbox_credentials.json`

The refresh token doesn't expire, so you only need to do this once.

## Usage

### Basic Download

```bash
python dropbox_download.py -o /path/to/output "DROPBOX_SHARED_URL"
```

### Dry Run (list files without downloading)

```bash
python dropbox_download.py --dry-run "DROPBOX_SHARED_URL"
```

### Using a Short-lived Token (alternative to OAuth)

If you prefer not to set up OAuth, you can use a short-lived access token:

```bash
python dropbox_download.py --token YOUR_ACCESS_TOKEN -o /path/to/output "URL"
```

Note: Access tokens expire after ~4 hours.

## Examples

```bash
# Download a shared folder
python dropbox_download.py -o ./downloads "https://www.dropbox.com/scl/fo/abc123/..."

# Preview what will be downloaded
python dropbox_download.py --dry-run "https://www.dropbox.com/scl/fo/abc123/..."

# Resume a failed download (just run the same command again)
python dropbox_download.py -o ./downloads "https://www.dropbox.com/scl/fo/abc123/..."
```

## Getting the Shared URL

1. Navigate to the folder in Dropbox web interface
2. Click **"Share"** button
3. Copy the link

The URL should look like:
```
https://www.dropbox.com/scl/fo/{folder_id}/{hash}/{path}?rlkey={key}&...
```

## Troubleshooting

### "This app is not valid" error

- Make sure you clicked **"Submit"** on the Permissions tab after enabling permissions
- Verify the app key is correct

### Token expired

If using OAuth with refresh token, it should auto-renew. If it fails:
```bash
python dropbox_auth.py --app-key YOUR_KEY --app-secret YOUR_SECRET
```

### Connection errors

The script automatically retries with exponential backoff. If downloads keep failing:
- Check your internet connection
- Try again later (Dropbox may be rate limiting)

### Resuming downloads

Just run the same command again. The script checks file sizes and skips already-downloaded files.

## File Structure

```
~/.dropbox_credentials.json   # OAuth credentials (created by dropbox_auth.py)
dropbox_auth.py               # One-time OAuth setup
dropbox_download.py           # Main download script
```
