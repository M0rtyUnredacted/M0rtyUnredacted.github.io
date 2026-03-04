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


def _dismiss_joyride(page) -> None:
    """Dismiss TikTok's react-joyride onboarding overlay if it appears."""
    overlay_sel = "[data-test-id='overlay'], .react-joyride__overlay"
    try:
        page.wait_for_selector(overlay_sel, timeout=4_000)
    except Exception:
        return  # no overlay — nothing to do

    # Try keyboard dismiss first
    page.keyboard.press("Escape")
    time.sleep(0.5)

    # Try visible skip/close buttons
    for btn_sel in (
        "button:has-text('Skip')",
        "button:has-text('Got it')",
        "button:has-text('Done')",
        "[aria-label='Close']",
        "button[class*='close']",
    ):
        try:
            btn = page.locator(btn_sel).first
            if btn.is_visible():
                btn.click(force=True)
                time.sleep(0.5)
                break
        except Exception:
            continue

    # Nuclear fallback: remove the portal node from the DOM entirely
    if page.locator(overlay_sel).is_visible():
        page.evaluate(
            "() => { const el = document.getElementById('react-joyride-portal'); "
            "if (el) el.remove(); }"
        )


def _safe_click(page, locator, ui_log, label: str, timeout: int = 10_000):
    """Click a locator; if an overlay intercepts, retry with force=True."""
    try:
        locator.click(timeout=timeout)
    except Exception as exc:
        if "intercepts pointer events" in str(exc):
            ui_log(f"TikTok: overlay blocked '{label}' — using force click")
            _dismiss_joyride(page)
            locator.click(force=True, timeout=timeout)
        else:
            raise


def _screenshot_on_fail(page, label: str) -> str | None:
    """Save a screenshot to temp/ and return the path (or None)."""
    try:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        path = os.path.join(TEMP_DIR, f"fail_{label}_{ts}.png")
        os.makedirs(TEMP_DIR, exist_ok=True)
        page.screenshot(path=path, full_page=True)
        log.info("Failure screenshot saved: %s", path)
        return path
    except Exception:
        return None


def _tiktok_upload(page, mp4_path: str, caption: str, schedule_dt: datetime, ui_log):
    ui_log("TikTok: navigating to Studio ...")
    page.goto(TIKTOK_STUDIO_URL, wait_until="domcontentloaded", timeout=60_000)

    # Wait for the upload UI to actually render instead of a blind sleep
    try:
        page.wait_for_selector(
            "input[type='file'], button:has-text('Select files'), "
            "button:has-text('Upload'), div[class*='upload']",
            timeout=15_000,
        )
    except Exception:
        # Check if we got redirected to a login page
        current = page.url
        if "/login" in current or "/passport" in current:
            _screenshot_on_fail(page, "login_redirect")
            raise RuntimeError(
                f"TikTok redirected to login ({current}). "
                "Your session may have expired — log in to TikTok in Chrome "
                "and restart the app."
            )
        _screenshot_on_fail(page, "upload_ui_missing")
        raise RuntimeError(
            f"Upload UI did not appear within 15 s (url={current}). "
            "Check the screenshot in temp/ for details."
        )

    _dismiss_joyride(page)

    # ── Upload ────────────────────────────────────────────────────────────────
    # TikTok Studio shows a file-picker popup when the upload area is clicked.
    # expect_file_chooser intercepts it at the browser level whether it is a
    # hidden <input type="file"> or a JS-triggered native dialog.
    ui_log("TikTok: triggering file upload ...")
    try:
        with page.expect_file_chooser(timeout=20_000) as fc_info:
            page.locator(
                "button:has-text('Select files'), "
                "button:has-text('Upload'), "
                "div[class*='upload-btn'], "
                "div[class*='upload-card'], "
                "label[class*='upload'], "
                "input[type='file']"
            ).first.click()
        fc_info.value.set_files(mp4_path)
    except Exception:
        # Fallback: set files directly on the hidden file input
        ui_log("TikTok: file-chooser intercept failed, trying direct input ...")
        upload_input = page.locator("input[type='file']").first
        upload_input.wait_for(state="attached", timeout=10_000)
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
        _screenshot_on_fail(page, "upload_timeout")
        raise TimeoutError("TikTok upload did not finish within 5 minutes.")

    # ── Caption ───────────────────────────────────────────────────────────────
    ui_log("TikTok: filling caption ...")
    _safe_click(page, caption_field, ui_log, "caption_field")
    caption_field.fill("")
    page.keyboard.type(caption[:2200])
    time.sleep(1)

    # ── Schedule ──────────────────────────────────────────────────────────────
    ui_log("TikTok: setting schedule ...")
    schedule_toggle = page.locator(
        "label:has-text('Schedule'), input[value='schedule'], [aria-label*='Schedule']"
    ).first
    if schedule_toggle.is_visible():
        _safe_click(page, schedule_toggle, ui_log, "schedule_toggle")
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
    _safe_click(page, post_btn, ui_log, "post_button")
    time.sleep(3)

    confirm = page.locator("button:has-text('Confirm'), button:has-text('OK')").first
    if confirm.is_visible():
        _safe_click(page, confirm, ui_log, "confirm_button")
        time.sleep(2)

    ui_log("TikTok: post scheduled.")
