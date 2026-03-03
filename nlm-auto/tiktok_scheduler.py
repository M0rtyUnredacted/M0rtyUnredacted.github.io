"""TikTok Scheduler — every 10 min.

Pipeline per new MP4 in the Drive TikTok folder:
  1. List MP4s in Drive TikTok folder
  2. For each new MP4:
     a. Download MP4 locally
     b. Read paired .md sidecar file for caption (same name, .md extension)
     c. Open TikTok Studio upload page in Chrome
     d. Upload MP4, fill caption, schedule at (last_post + tiktok_gap_hours)
     e. Confirm schedule
     f. Mark as processed in state; update last_post timestamp
"""

import logging
import os
import time
from datetime import datetime, timedelta

import chrome_client
from drive_client import DriveClient
from state import load_state, save_state

log = logging.getLogger(__name__)

TIKTOK_STUDIO_URL = "https://www.tiktok.com/creator-center/upload"


def run(config: dict, ui_log):
    """Entry point called by the scheduler."""
    drive = DriveClient()
    state = load_state()
    processed = set(state.get("processed_tiktok", []))

    mp4s = drive.list_mp4s(config["tiktok_folder_id"])
    new_mp4s = [f for f in mp4s if f["id"] not in processed]

    if not new_mp4s:
        ui_log("TikTok Scheduler: no new videos.")
        return

    ui_log(f"TikTok Scheduler: {len(new_mp4s)} new video(s) to schedule.")

    # Sort by modified time so we post in order
    new_mp4s.sort(key=lambda f: f.get("modifiedTime", ""))

    for mp4 in new_mp4s:
        try:
            _process_video(mp4, config, drive, state, ui_log)
        except Exception as exc:
            log.exception("TikTok Scheduler: failed on %s", mp4["name"])
            raise RuntimeError(f"TikTok failed on '{mp4['name']}': {exc}") from exc


def _process_video(mp4: dict, config: dict, drive: DriveClient, state: dict, ui_log):
    file_id = mp4["id"]
    name = mp4["name"]
    ui_log(f"TikTok: processing '{name}' ...")

    downloads_dir = os.path.abspath(config.get("downloads_dir", "downloads"))
    os.makedirs(downloads_dir, exist_ok=True)

    # Download MP4
    local_mp4 = os.path.join(downloads_dir, name)
    drive.download_file(file_id, local_mp4)
    ui_log(f"TikTok: downloaded '{name}'.")

    # Find paired sidecar .md in the same folder
    caption = _get_caption(drive, config["tiktok_folder_id"], name)
    ui_log(f"TikTok: caption = {caption[:60]}..." if len(caption) > 60 else f"TikTok: caption = {caption}")

    # Determine schedule time: last_post + gap
    gap_hours = config.get("tiktok_gap_hours", 5)
    last_post_str = state.get("last_tiktok_post")
    if last_post_str:
        last_post = datetime.fromisoformat(last_post_str)
        schedule_dt = last_post + timedelta(hours=gap_hours)
        # If that's in the past, push forward to now + gap
        if schedule_dt < datetime.now():
            schedule_dt = datetime.now() + timedelta(hours=gap_hours)
    else:
        schedule_dt = datetime.now() + timedelta(hours=gap_hours)

    ui_log(f"TikTok: scheduling at {schedule_dt.strftime('%Y-%m-%d %H:%M')} ...")

    # Browser automation
    page = chrome_client.new_page(config["chrome_debug_url"])
    try:
        _tiktok_upload(page, local_mp4, caption, schedule_dt, ui_log)
    finally:
        page.close()

    # Update state
    state.setdefault("processed_tiktok", []).append(file_id)
    state["last_tiktok_post"] = schedule_dt.isoformat()
    save_state(state)

    # Clean up local file
    os.remove(local_mp4)
    ui_log(f"TikTok: '{name}' scheduled successfully.")


def _get_caption(drive: DriveClient, folder_id: str, mp4_name: str) -> str:
    """Look for a .md sidecar with the same stem as *mp4_name*."""
    stem = os.path.splitext(mp4_name)[0]
    md_files = drive.list_files(folder_id)
    for f in md_files:
        if f["name"] in (stem + ".md", stem + ".txt"):
            try:
                return drive.read_plain_text(f["id"])
            except Exception as exc:
                log.warning("Could not read sidecar %s: %s", f["name"], exc)
    return f"#{stem.replace(' ', '')} #MortyUnredacted"


def _tiktok_upload(page, mp4_path: str, caption: str, schedule_dt: datetime, ui_log):
    """Upload video to TikTok Studio and schedule it."""

    ui_log("TikTok: navigating to Studio ...")
    page.goto(TIKTOK_STUDIO_URL, wait_until="networkidle", timeout=60_000)
    time.sleep(3)

    # ── Upload file ───────────────────────────────────────────────────────────
    ui_log("TikTok: uploading video file ...")
    upload_input = page.locator("input[type='file']").first
    upload_input.wait_for(timeout=20_000)
    upload_input.set_input_files(mp4_path)

    # Wait for upload progress to complete
    ui_log("TikTok: waiting for upload to complete ...")
    deadline = time.time() + 300
    while time.time() < deadline:
        # Look for the caption/text field — appears when upload finishes
        caption_field = page.locator(
            "[data-text='true'], [contenteditable='true'][class*='caption'], "
            "textarea[placeholder*='caption'], textarea[placeholder*='Caption']"
        ).first
        if caption_field.is_visible():
            break
        time.sleep(3)
    else:
        raise TimeoutError("TikTok upload did not finish within 5 minutes.")

    # ── Caption ───────────────────────────────────────────────────────────────
    ui_log("TikTok: filling caption ...")
    caption_field.click()
    caption_field.fill("")
    page.keyboard.type(caption[:2200])  # TikTok caption limit
    time.sleep(1)

    # ── Schedule ─────────────────────────────────────────────────────────────
    ui_log("TikTok: setting schedule ...")
    # Click "Schedule" radio / toggle
    schedule_toggle = page.locator(
        "label:has-text('Schedule'), input[value='schedule'], [aria-label*='Schedule']"
    ).first
    if schedule_toggle.is_visible():
        schedule_toggle.click()
        time.sleep(1)

    # Fill date
    date_input = page.locator("input[type='date'], input[placeholder*='date'], input[placeholder*='Date']").first
    if date_input.is_visible():
        date_input.fill(schedule_dt.strftime("%Y-%m-%d"))
        time.sleep(0.5)

    # Fill time
    time_input = page.locator("input[type='time'], input[placeholder*='time'], input[placeholder*='Time']").first
    if time_input.is_visible():
        time_input.fill(schedule_dt.strftime("%H:%M"))
        time.sleep(0.5)

    # ── Post / Schedule button ────────────────────────────────────────────────
    post_btn = page.locator(
        "button:has-text('Schedule'), button:has-text('Post'), button:has-text('Submit')"
    ).last
    post_btn.wait_for(timeout=10_000)
    post_btn.click()
    time.sleep(3)

    # Confirm if dialog appears
    confirm = page.locator("button:has-text('Confirm'), button:has-text('OK')").first
    if confirm.is_visible():
        confirm.click()
        time.sleep(2)

    ui_log("TikTok: post scheduled.")
