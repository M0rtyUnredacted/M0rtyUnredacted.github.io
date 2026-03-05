# NLM Automation App — Setup Guide

This guide walks you through setting up the NLM Automation App from scratch on a Windows machine.

---

## Prerequisites

| Requirement | Notes |
|---|---|
| Windows 10 / 11 | The batch scripts require Windows |
| Python 3.9+ | [python.org/downloads](https://www.python.org/downloads/) — check **"Add Python to PATH"** during install |
| Google Chrome | [google.com/chrome](https://www.google.com/chrome/) — standard install |
| Google account | Must have access to Google Drive and NotebookLM |
| Google Cloud project | Free tier is sufficient |

---

## Step 1 — Installation

1. Download or clone this repository so that `nlm-auto/` and its files are present.
2. Copy the entire `nlm-auto/` folder to `C:\nlm_app\`:
   ```
   C:\nlm_app\
     run.bat
     install.bat
     main.py
     config_template.json
     requirements.txt
     ... (all other files)
   ```
3. Open a Command Prompt, `cd C:\nlm_app`, and run:
   ```
   install.bat
   ```
   This installs Python packages and the Playwright browser. It only needs to run once.

---

## Step 2 — Google Drive Setup

You need **three** Google Drive folders:

| Folder | Purpose |
|---|---|
| **Query Docs folder** | You drop `.gdoc` files here; the app picks them up and sends them to NotebookLM |
| **TikTok Ready folder** | Drop `.mp4` files (+ matching `.md`/`.txt` caption sidecar) here — the app picks them up and schedules them on TikTok |
| **TikTok Posted folder** | After a video is successfully scheduled, the app moves both the `.mp4` and its caption sidecar here automatically |

**To create a folder and get its ID:**
1. Go to [drive.google.com](https://drive.google.com) and create the folder.
2. Open the folder. The URL looks like:
   ```
   https://drive.google.com/drive/folders/1ABC123XYZ...
   ```
3. The long string after `/folders/` is the **folder ID**. Copy it.

---

## Step 3 — Google Service Account

The app uses a **service account** (not your personal login) to access Drive via API.

1. Go to [console.cloud.google.com](https://console.cloud.google.com/) and create a new project (or select an existing one).
2. Enable the **Google Drive API**:
   - Navigate to **APIs & Services → Library**
   - Search for "Google Drive API" and click **Enable**
3. Create a service account:
   - Navigate to **APIs & Services → Credentials → Create Credentials → Service Account**
   - Give it a name (e.g., `nlm-automation`), click **Done**
4. Download the JSON key:
   - Click the service account you just created
   - Go to the **Keys** tab → **Add Key → Create New Key → JSON**
   - Save the downloaded file as `credentials.json`
   - Copy `credentials.json` to `C:\nlm_app\`
5. Share both Drive folders with the service account email (looks like `nlm-automation@your-project.iam.gserviceaccount.com`):
   - Right-click the folder in Drive → **Share**
   - Paste the service account email, set permission to **Editor**, click **Send**

---

## Step 4 — NotebookLM Notebook

1. Go to [notebooklm.google.com](https://notebooklm.google.com/) and create a new notebook.
2. Open the notebook. The URL looks like:
   ```
   https://notebooklm.google.com/notebook/abc123-def456-...
   ```
3. Copy the full URL — you will paste it into `config.json`.

---

## Step 5 — Gmail App Password (for failure notifications)

If you want email alerts when something goes wrong:

1. Go to your Google account → **Security → 2-Step Verification** (enable if not already on).
2. Search for **"App passwords"** in the Security settings.
3. Create an app password for **Mail / Windows Computer**.
4. Copy the 16-character password (shown once, spaces included are fine).

---

## Step 6 — config.json

Copy the template and fill it in:

```
copy C:\nlm_app\config_template.json C:\nlm_app\config.json
notepad C:\nlm_app\config.json
```

Fill in every value:

```json
{
  "google_drive": {
    "query_docs_folder_id":  "paste-your-query-docs-folder-id-here",
    "tiktok_ready_folder_id":  "paste-your-tiktok-ready-folder-id-here",
    "tiktok_posted_folder_id": "paste-your-tiktok-posted-folder-id-here",
    "poll_interval_minutes": 15
  },

  "notebooklm": {
    "notebook_url": "https://notebooklm.google.com/notebook/YOUR-NOTEBOOK-ID",
    "chrome_profile_path": "C:\\Users\\YourName\\AppData\\Local\\Google\\Chrome\\User Data\\Default",
    "style_doc_id": ""
  },

  "tiktok": {
    "post_interval_hours": 5,
    "chrome_profile_path": "C:\\Users\\YourName\\AppData\\Local\\Google\\Chrome\\User Data\\Default"
  },

  "notifications": {
    "email": "you@gmail.com",
    "notify_on_failure": true,
    "gmail_app_password": "xxxx xxxx xxxx xxxx"
  },

  "daily_quota": {
    "max_nlm_videos_per_day": 6
  }
}
```

### How to find your `chrome_profile_path`

This is the most common setup mistake. Follow these steps exactly:

1. Press **Win + R**, type the following, and press Enter:
   ```
   %LOCALAPPDATA%\Google\Chrome\User Data
   ```
2. Windows Explorer opens. You will see folders like `Default`, `Profile 1`, `Profile 2`, etc.
3. Your active Chrome profile folder is usually `Default` (if you have only one Chrome profile) or `Profile 1` (if you added extra profiles).
4. The full path to use is:
   ```
   C:\Users\YourName\AppData\Local\Google\Chrome\User Data\Default
   ```
   Replace `YourName` with your actual Windows username, and `Default` with whichever profile folder is yours.
5. Paste this path as the value for **both** `notebooklm.chrome_profile_path` **and** `tiktok.chrome_profile_path`.

> **Tip:** To confirm your username, open Command Prompt and type `echo %USERNAME%`.

> **Note:** Use double backslashes `\\` inside the JSON string (e.g., `C:\\Users\\marka\\...`).

---

## Step 7 — First Run

Run the app:
```
cd C:\nlm_app
run.bat
```

Expected output:
```
=== NLM Automation App ===

Dependencies already installed.
Checking Chrome debug port 9222 ...
Port 9222 not responding -- launching Chrome ...
  User data: C:\Users\marka\AppData\Local\Google\Chrome\User Data
  Profile  : Default
Waiting for Chrome to bind port 9222 ...
Chrome ready on port 9222.

Starting NLM Automation App ...
Gradio UI -> http://localhost:7860
```

Open [http://localhost:7860](http://localhost:7860) in your browser to see the live status log.

---

## Troubleshooting

### `User data: Default` / `The system cannot find the drive specified.`

Your `chrome_profile_path` is empty or invalid in `config.json`. Follow Step 6 above to find and set the correct path.

### `Chrome did not bind port 9222 after 14 seconds`

- Make sure no other Chrome window is open with the same profile before running `run.bat`. The script kills existing Chrome processes, but sometimes a hung process remains.
- Run manually to test:
  ```
  curl http://127.0.0.1:9222/json/version
  ```
- Try launching Chrome manually:
  ```
  "C:\Program Files (x86)\Google\Chrome\Application\chrome.exe" --remote-debugging-port=9222 --user-data-dir="C:\Users\YourName\AppData\Local\Google\Chrome\User Data" --profile-directory="Default"
  ```

### `credentials.json not found`

Copy your downloaded service account JSON file to `C:\nlm_app\credentials.json`.

### `pip install failed. Is Python 3 installed and on PATH?`

Reinstall Python from [python.org](https://www.python.org/downloads/) and check the **"Add Python to PATH"** box during installation.

### The Gradio UI opens but nothing happens

- Check that `query_docs_folder_id` in `config.json` is correct and that the service account has **Editor** access to that folder.
- Drop a Google Doc into the query docs folder and wait up to 15 minutes for the poller to pick it up.
