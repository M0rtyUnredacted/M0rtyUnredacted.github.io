"""TikTok Scheduler — every 10 min.

Pipeline per new MP4 in Drive tiktok_manual_folder:
  1. Download MP4 to temp dir
  2. Read paired .md sidecar for caption
  3. Open TikTok Studio upload page in Chrome (CDP on 127.0.0.1:9222)
  4. Upload MP4, fill caption, schedule at max(now+20min, last_post+5h)
  5. Mark as scheduled in SQLite
"""

import logging
import os
import time
from datetime import datetime, timedelta

import chrome_client
import db
from drive_client import DriveClient

log = logging.getLogger(__name__)

TEMP_DIR = os.path.join(os.path.dirname(__file__), "temp")
CDP_URL = "http://127.0.0.1:9222"
TIKTOK_STUDIO_URL = "https://www.tiktok.com/creator-center/upload"


def run(config: dict, ui_log):
    db.init()
    drive = DriveClient()

    folder_id = config["google_drive"]["tiktok_manual_folder_id"]
    mp4s = drive.list_mp4s(folder_id)
    new_mp4s = [f for f in mp4s if not db.is_tiktok_processed(f["id"])]

    if not new_mp4s:
        ui_log("TikTok Scheduler: no new videos.")
        return

    ui_log(f"TikTok Scheduler: {len(new_mp4s)} new video(s) to schedule.")
    new_mp4s.sort(key=lambda f: f.get("modifiedTime", ""))

    for mp4 in new_mp4s:
        try:
            _process_video(mp4, config, drive, ui_log)
        except Exception as exc:
            log.exception("TikTok Scheduler: failed on '%s'", mp4["name"])
            db.mark_tiktok_failed(mp4["id"], mp4["name"], str(exc))
            raise RuntimeError(f"TikTok failed on '{mp4['name']}': {exc}") from exc


def _process_video(mp4: dict, config: dict, drive: DriveClient, ui_log):
    file_id = mp4["id"]
    name = mp4["name"]
    ui_log(f"TikTok: processing '{name}' ...")

    os.makedirs(TEMP_DIR, exist_ok=True)
    local_mp4 = os.path.join(TEMP_DIR, name)
    drive.download_file(file_id, local_mp4)
    ui_log(f"TikTok: downloaded '{name}'.")

    folder_id = config["google_drive"]["tiktok_manual_folder_id"]
    caption = _get_caption(drive, folder_id, name)
    ui_log(f"TikTok: caption = {caption[:80]}{'...' if len(caption) > 80 else ''}")

    gap_hours = config.get("tiktok", {}).get("post_interval_hours", 5)
    last_post_str = db.last_tiktok_scheduled_time()
    if last_post_str:
        last_post = datetime.fromisoformat(last_post_str)
        schedule_dt = max(
            datetime.now() + timedelta(minutes=20),
            last_post + timedelta(hours=gap_hours),
        )
    else:
        schedule_dt = datetime.now() + timedelta(hours=gap_hours)

    ui_log(f"TikTok: scheduling at {schedule_dt.strftime('%Y-%m-%d %H:%M')} ...")

    page = chrome_client.new_page(CDP_URL)
    try:
        _tiktok_upload(page, local_mp4, caption, schedule_dt, ui_log)
    finally:
        page.close()

    db.mark_tiktok_scheduled(file_id, name, schedule_dt.isoformat())
    os.remove(local_mp4)
    ui_log(f"TikTok: '{name}' scheduled.")


def _get_caption(drive: DriveClient, folder_id: str, mp4_name: str) -> str:
    """Priority: sidecar .md > sidecar .txt > filename-based fallback."""
    stem = os.path.splitext(mp4_name)[0]
    for f in drive.list_files(folder_id):
        if f["name"] in (stem + ".md", stem + ".txt"):
            try:
                return drive.read_plain_text(f["id"])
            except Exception as exc:
                log.warning("Could not read sidecar %s: %s", f["name"], exc)
    return f"#{stem.replace(' ', '')} #MortyUnredacted"


def _tiktok_upload(page, mp4_path: str, caption: str, schedule_dt: datetime, ui_log):
    ui_log("TikTok: navigating to Studio ...")
    page.goto(TIKTOK_STUDIO_URL, wait_until="networkidle", timeout=60_000)
    time.sleep(3)

    # ── Upload ────────────────────────────────────────────────────────────────
    ui_log("TikTok: uploading video ...")
    upload_input = page.locator("input[type='file']").first
    upload_input.wait_for(timeout=20_000)
    upload_input.set_input_files(mp4_path)

    ui_log("TikTok: waiting for upload to complete ...")
    deadline = time.time() + 300
    while time.time() < deadline:
        caption_field = page.locator(
            "[data-text='true'], [contenteditable='true'][class*='caption'], "
            "textarea[placeholder*='caption'], textarea[placeholder*='Caption'], "
            "div[class*='editor'][contenteditable='true']"
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
    page.keyboard.type(caption[:2200])
    time.sleep(1)

    # ── Schedule ──────────────────────────────────────────────────────────────
    ui_log("TikTok: setting schedule ...")
    schedule_toggle = page.locator(
        "label:has-text('Schedule'), input[value='schedule'], [aria-label*='Schedule']"
    ).first
    if schedule_toggle.is_visible():
        schedule_toggle.click()
        time.sleep(1)

    date_input = page.locator("input[type='date'], input[placeholder*='date']").first
    if date_input.is_visible():
        date_input.fill(schedule_dt.strftime("%Y-%m-%d"))
        time.sleep(0.5)

    time_input = page.locator("input[type='time'], input[placeholder*='time']").first
    if time_input.is_visible():
        time_input.fill(schedule_dt.strftime("%H:%M"))
        time.sleep(0.5)

    # ── Submit ────────────────────────────────────────────────────────────────
    post_btn = page.locator(
        "button:has-text('Schedule'), button:has-text('Post'), button:has-text('Submit')"
    ).last
    post_btn.wait_for(timeout=10_000)
    post_btn.click()
    time.sleep(3)

    confirm = page.locator("button:has-text('Confirm'), button:has-text('OK')").first
    if confirm.is_visible():
        confirm.click()
        time.sleep(2)

    ui_log("TikTok: post scheduled.")
