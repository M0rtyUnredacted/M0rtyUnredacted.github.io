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


class TiktokRateLimitError(RuntimeError):
    """TikTok is throttling uploads (/upload/unavailable).  Transient — do NOT
    mark the video as permanently failed; it will be retried on the next poll."""

TEMP_DIR = os.path.join(os.path.dirname(__file__), "temp")
CDP_URL = "http://127.0.0.1:9222"
TIKTOK_STUDIO_URL = "https://www.tiktok.com/tiktokstudio/upload"


def run(config: dict, ui_log):
    db.init()
    drive = DriveClient()

    folder_id = config["google_drive"]["tiktok_manual_folder_id"]
    mp4s = drive.list_mp4s(folder_id)
    new_mp4s = [f for f in mp4s if not db.is_tiktok_processed(f["id"])]

    if not new_mp4s:
        ui_log("TikTok Scheduler: no new videos.")
        return

    new_mp4s.sort(key=lambda f: f.get("modifiedTime", ""))
    pending = len(new_mp4s)
    ui_log(f"TikTok Scheduler: {pending} video(s) pending.")

    # Process all queued videos, but pause 30 s between uploads so TikTok's
    # server has time to finish processing the previous post (instant tab
    # reopening triggers /upload/unavailable even without a hard rate limit).
    INTER_UPLOAD_PAUSE = 30  # seconds

    for i, mp4 in enumerate(new_mp4s):
        if i > 0:
            ui_log(f"TikTok: pausing {INTER_UPLOAD_PAUSE}s before next upload ...")
            time.sleep(INTER_UPLOAD_PAUSE)
        try:
            _process_video(mp4, config, drive, ui_log)
        except TiktokRateLimitError as exc:
            # Transient — TikTok is throttling.  Don't permanently fail the video.
            ui_log(f"TikTok: rate-limited on '{mp4['name']}' — will retry next poll.")
            log.warning("TikTok rate limit on '%s': %s", mp4["name"], exc)
            raise RuntimeError(f"TikTok rate-limited: {exc}") from exc
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
    page = chrome_client.new_page(CDP_URL)
    try:
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
        _tiktok_upload(page, local_mp4, caption, schedule_dt, ui_log)
        db.mark_tiktok_scheduled(file_id, name, schedule_dt.isoformat())
        ui_log(f"TikTok: '{name}' scheduled.")
    finally:
        page.close()
        try:
            os.remove(local_mp4)
        except FileNotFoundError:
            pass


def _get_caption(drive: DriveClient, folder_id: str, mp4_name: str) -> str:
    """Priority: sidecar .md > sidecar .txt > filename-based fallback."""
    stem = os.path.splitext(mp4_name)[0]
    for f in drive.list_files(folder_id):
        if f["name"] in (stem + ".md", stem + ".txt"):
            try:
                return drive.read_plain_text(f["id"])
            except Exception as exc:
                log.warning("Could not read sidecar %s: %s", f["name"], exc)
    return _caption_from_filename(stem)


def _caption_from_filename(stem: str) -> str:
    """Generate a readable caption + 5 hashtags from a filename stem.

    Rules to stay TikTok TOS-safe:
    - No misleading claims, no banned/restricted topic tags
    - Hashtags are derived from the filename words plus safe engagement tags
    """
    import re

    # Strip leading date prefixes: 2024-01-15_, 20240115_
    cleaned = re.sub(r"^(\d{4}[-_]\d{2}[-_]\d{2}[-_]?|\d{8}[-_]?)", "", stem)
    # Replace separators with spaces, collapse whitespace
    cleaned = re.sub(r"[_\-]+", " ", cleaned).strip()
    title = cleaned.title() if cleaned else stem

    # Extract meaningful words (>3 chars, not stop-words) for topic hashtags
    _STOP = {"a", "an", "the", "and", "or", "but", "in", "on", "at",
             "to", "for", "of", "with", "is", "are", "was", "were", "be",
             "this", "that", "from", "its", "it"}
    words = [
        w for w in re.findall(r"[A-Za-z]+", cleaned)
        if len(w) > 3 and w.lower() not in _STOP
    ]

    # Up to 3 topic hashtags from filename, then fill with safe baseline tags
    topic_tags = [f"#{w.capitalize()}" for w in words[:3]]
    baseline_tags = ["#MortyUnredacted", "#FYP", "#ForYouPage"]
    seen: set[str] = set()
    all_tags: list[str] = []
    for tag in topic_tags + baseline_tags:
        if tag.lower() not in seen:
            seen.add(tag.lower())
            all_tags.append(tag)
        if len(all_tags) == 5:
            break

    caption = f"{title}\n\n{' '.join(all_tags)}"
    log.debug("Generated caption from filename '%s': %s", stem, caption)
    return caption


def _dismiss_draft_recovery(page, ui_log) -> None:
    """Dismiss TikTok's 'A video you were editing wasn't saved' dialog.

    This appears when a previous session was killed mid-upload/edit.
    We always discard the draft so the upload flow starts fresh.
    Looking ahead: after clicking Discard, TikTok briefly re-renders —
    callers should wait ~1s before continuing.
    """
    # Detect the dialog by its distinctive text
    dialog_sel = (
        "div:has-text('wasn\\'t saved'), "
        "div:has-text('Continue editing'), "
        "[class*='modal']:has-text('editing')"
    )
    try:
        page.wait_for_selector(dialog_sel, timeout=4_000)
    except Exception:
        return  # no draft recovery dialog — nothing to do

    ui_log("TikTok: draft recovery dialog detected — discarding draft ...")
    # Prefer Discard/Leave/No so we start with a clean upload form
    for btn_text in ("Discard", "Leave", "No", "Cancel"):
        try:
            btn = page.locator(f"button:has-text('{btn_text}')").first
            if btn.is_visible(timeout=1_000):
                btn.click()
                time.sleep(1.5)  # wait for TikTok to re-render upload page
                ui_log("TikTok: draft discarded.")
                return
        except Exception:
            continue

    # Fallback: if only "Continue editing" is visible, click it so we're not
    # stuck — the upload loop will handle the already-loaded editor state.
    try:
        btn = page.locator("button:has-text('Continue')").first
        if btn.is_visible(timeout=1_000):
            ui_log("TikTok: resuming existing draft (no Discard button found).")
            btn.click()
            time.sleep(1.5)
    except Exception:
        pass


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


def _wait_for_upload_frame(page, timeout_ms: int = 30_000):
    """Find the frame (main page or iframe) that contains the upload file input.

    TikTok Studio embeds the upload form inside an iframe on current versions.
    Searches page.frames every second until input[type='file'] is found or
    the timeout expires.  Returns the Frame object, or None on timeout.
    """
    deadline = time.time() + timeout_ms / 1000
    while time.time() < deadline:
        current_url = page.url
        if "/unavailable" in current_url:
            raise TiktokRateLimitError(
                f"TikTok redirected to {current_url} — upload rate limit in effect. "
                "Wait a few minutes before the next upload."
            )
        for frame in page.frames:
            try:
                if frame.locator("input[type='file']").count() > 0:
                    log.debug("Upload file input found in frame: %s", frame.url)
                    return frame
            except Exception:
                continue
        time.sleep(1)
    return None


def _tiktok_upload(page, mp4_path: str, caption: str, schedule_dt: datetime, ui_log):
    ui_log("TikTok: navigating to Studio ...")
    page.goto(TIKTOK_STUDIO_URL, wait_until="domcontentloaded", timeout=60_000)
    _dismiss_joyride(page)
    _dismiss_draft_recovery(page, ui_log)  # handles "wasn't saved" dialog

    # TikTok Studio loads the upload form inside an iframe.
    # Scan all frames (main + iframes) for the file input element.
    ui_log("TikTok: waiting for upload UI ...")
    frame = _wait_for_upload_frame(page, timeout_ms=30_000)
    if frame is None:
        current = page.url
        if "/unavailable" in current:
            raise TiktokRateLimitError(
                f"TikTok upload unavailable ({current}) — rate limit in effect."
            )
        if "/login" in current or "/passport" in current:
            _screenshot_on_fail(page, "login_redirect")
            raise RuntimeError(
                f"TikTok redirected to login ({current}). "
                "Your session may have expired — log in to TikTok in Chrome "
                "and restart the app."
            )
        _screenshot_on_fail(page, "upload_ui_missing")
        raise RuntimeError(
            f"Upload file input not found in any frame within 30 s "
            f"(url={current}). Check temp/fail_upload_ui_missing_*.png."
        )

    # ── Upload ────────────────────────────────────────────────────────────────
    # expect_file_chooser is a browser-level hook — works even when the click
    # originates inside an iframe.
    ui_log("TikTok: triggering file upload ...")
    try:
        with page.expect_file_chooser(timeout=20_000) as fc_info:
            frame.locator(
                "button:has-text('Select files'), "
                "div[class*='upload-btn'], "
                "div[class*='upload-card'], "
                "label[class*='upload'], "
                "input[type='file']"
            ).first.click()
        fc_info.value.set_files(mp4_path)
    except Exception:
        # Fallback: set files directly on the hidden file input
        ui_log("TikTok: file-chooser intercept failed, trying direct input ...")
        upload_input = frame.locator("input[type='file']").first
        upload_input.wait_for(state="attached", timeout=10_000)
        upload_input.set_input_files(mp4_path)

    ui_log("TikTok: waiting for upload to complete ...")
    deadline = time.time() + 300
    while time.time() < deadline:
        caption_field = frame.locator(
            # Most specific first: placeholder text on the caption div
            "div[contenteditable='true'][data-placeholder*='caption' i], "
            "div[contenteditable='true'][data-placeholder*='describe' i], "
            # Class-based fallbacks
            "[contenteditable='true'][class*='caption'], "
            "textarea[placeholder*='caption' i], "
            # Broadest last — only reached if nothing above matches
            "div[class*='editor'][contenteditable='true']"
        ).first
        if caption_field.is_visible():
            break
        time.sleep(3)
    else:
        _screenshot_on_fail(page, "upload_timeout")
        raise TimeoutError("TikTok upload did not finish within 5 minutes.")

    # ── Caption ───────────────────────────────────────────────────────────────
    # TikTok's editor is a Slate/Draft.js contenteditable div.
    # Re-query fresh (don't reuse the loop reference — TikTok may have
    # re-rendered after upload finished, leaving the old locator stale).
    # Small pause first to let the post-upload UI finish settling.
    time.sleep(2)
    caption_field = frame.locator(
        "[data-text='true'], [contenteditable='true'][class*='caption'], "
        "textarea[placeholder*='caption'], textarea[placeholder*='Caption'], "
        "div[class*='editor'][contenteditable='true']"
    ).first
    ui_log("TikTok: filling caption ...")
    _screenshot_on_fail(page, "before_caption")  # diagnostic: see the page state
    _safe_click(page, caption_field, ui_log, "caption_field")
    caption_field.press("Control+a")
    caption_field.press("Delete")
    page.keyboard.type(caption[:2200])
    time.sleep(1)

    # ── Schedule ──────────────────────────────────────────────────────────────
    ui_log("TikTok: setting schedule ...")
    _screenshot_on_fail(page, "before_schedule")  # diagnostic: see schedule UI state

    # TikTok Studio uses a radio-button / switch group to pick "Schedule" vs
    # "Publish now".  Try a broad set of selectors covering known UI variants.
    schedule_toggle = frame.locator(
        "label:has-text('Schedule'), "
        "input[value='schedule'], "
        "[aria-label*='Schedule'], "
        "div[class*='schedule']:has-text('Schedule'), "
        "span:has-text('Schedule')"
    ).first

    try:
        schedule_toggle.wait_for(state="visible", timeout=8_000)
        _safe_click(page, schedule_toggle, ui_log, "schedule_toggle")
        time.sleep(1)
    except Exception:
        _screenshot_on_fail(page, "schedule_toggle_missing")
        raise RuntimeError(
            "TikTok schedule toggle not found. "
            "Check temp/fail_schedule_toggle_missing_*.png to see the page state. "
            "TikTok may have changed their UI — selectors need updating."
        )

    date_input = frame.locator("input[type='date'], input[placeholder*='date']").first
    try:
        date_input.wait_for(state="visible", timeout=5_000)
        date_input.fill(schedule_dt.strftime("%Y-%m-%d"))
        date_input.dispatch_event("input")
        date_input.dispatch_event("change")
        time.sleep(0.5)
    except Exception:
        _screenshot_on_fail(page, "date_picker_missing")
        raise RuntimeError(
            "TikTok date picker not found after enabling schedule toggle. "
            "Check temp/fail_date_picker_missing_*.png."
        )

    time_input = frame.locator("input[type='time'], input[placeholder*='time']").first
    try:
        time_input.wait_for(state="visible", timeout=5_000)
        time_input.fill(schedule_dt.strftime("%H:%M"))
        time_input.dispatch_event("input")
        time_input.dispatch_event("change")
        time.sleep(0.5)
    except Exception:
        _screenshot_on_fail(page, "time_picker_missing")
        raise RuntimeError(
            "TikTok time picker not found after enabling schedule toggle. "
            "Check temp/fail_time_picker_missing_*.png."
        )

    # ── Submit ────────────────────────────────────────────────────────────────
    # Only look for a Schedule/Submit button — NOT "Post", to avoid accidentally
    # publishing immediately if the schedule toggle failed to activate.
    post_btn = frame.locator(
        "button:has-text('Schedule'), button:has-text('Submit')"
    ).last
    try:
        post_btn.wait_for(state="visible", timeout=10_000)
    except Exception:
        _screenshot_on_fail(page, "submit_btn_missing")
        raise RuntimeError(
            "TikTok 'Schedule'/'Submit' button not found. "
            "Check temp/fail_submit_btn_missing_*.png."
        )
    _safe_click(page, post_btn, ui_log, "post_button")
    time.sleep(3)

    confirm = frame.locator("button:has-text('Confirm'), button:has-text('OK')").first
    if confirm.is_visible():
        _safe_click(page, confirm, ui_log, "confirm_button")
        time.sleep(2)

    ui_log("TikTok: post scheduled.")
