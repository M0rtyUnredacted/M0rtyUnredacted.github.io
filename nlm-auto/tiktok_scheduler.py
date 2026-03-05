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


class TiktokUploadTimeoutError(TiktokRateLimitError):
    """Upload iframe never showed the caption field within the timeout window.
    Likely a slow network or TikTok processing delay — transient, retry next poll."""

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
            # Covers TiktokRateLimitError AND TiktokUploadTimeoutError (subclass).
            # Both are transient — do NOT mark the video as permanently failed;
            # it will be retried on the next poll.
            reason = "rate-limited" if type(exc) is TiktokRateLimitError else "upload timed out"
            ui_log(f"TikTok: {reason} — will retry '{mp4['name']}' next poll.")
            log.warning("TikTok transient error on '%s': %s", mp4["name"], exc)
            return
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

        # TikTok only allows scheduling up to 10 days ahead.
        # If the computed slot exceeds that, cap it and warn — a backed-up queue
        # could silently push the date past TikTok's limit.
        _MAX_SCHEDULE_DAYS = 10
        _max_allowed = datetime.now() + timedelta(days=_MAX_SCHEDULE_DAYS - 1, hours=23)
        if schedule_dt > _max_allowed:
            ui_log(
                f"TikTok: schedule {schedule_dt:%Y-%m-%d %H:%M} exceeds 10-day limit "
                f"— capping to {_max_allowed:%Y-%m-%d %H:%M}"
            )
            schedule_dt = _max_allowed

        ui_log(f"TikTok: scheduling at {schedule_dt.strftime('%Y-%m-%d %H:%M')} ...")
        _tiktok_upload(page, local_mp4, caption, schedule_dt, ui_log)
        db.mark_tiktok_scheduled(file_id, name, schedule_dt.isoformat())
        ui_log(f"TikTok: '{name}' scheduled.")
    finally:
        try:
            page.close()
        except Exception:
            pass  # page may already be closed; don't mask the real exception
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


def _click_discard_btn(page, ui_log, label: str) -> bool:
    """Click a Discard/Leave button if visible. Returns True if clicked."""
    for btn_text in ("Discard", "Leave", "Delete", "Remove"):
        try:
            btn = page.locator(f"button:has-text('{btn_text}')").first
            if btn.is_visible(timeout=1_500):
                ui_log(f"TikTok: clicking '{btn_text}' ({label})")
                btn.click()
                time.sleep(1.5)
                return True
        except Exception:
            continue
    return False


def _dismiss_draft_recovery(page, ui_log) -> None:
    """Dismiss all draft/discard dialogs TikTok may show after a killed session.

    TikTok can show up to TWO layered dialogs:
      1. 'A video you were editing wasn't saved. Continue editing?'
         or 'You have an unfinished post' — with Continue / Discard
      2. 'Discard this post? Your video ... will be discarded permanently.'
         — confirmation modal with  Not now / Discard

    Both must be handled.  We also handle the case where only dialog 2
    is shown (TikTok sometimes skips step 1 and goes straight to confirm).

    Looking ahead at additional states:
    - 'Not now' on dialog 2 keeps the draft; we must NOT click it.
    - After both dismissals TikTok briefly re-renders; we add settle waits.
    - The dialogs live on the main page, NOT inside the upload iframe.
    - button:has-text() matches even when the button label is CSS-truncated.
    """
    # ── Phase 0: TUXModal sweep ───────────────────────────────────────────────
    # TikTok's design system uses class='TUXModal-overlay'.  CSS [class*='modal']
    # is case-sensitive and never matched this.  Run the sweep unconditionally
    # so any open TUXModal (draft discard, advisory, etc.) is closed first.
    _dismiss_any_tux_modal(page, ui_log)

    # ── Broad detection: any of the known non-TUX dialog variants ─────────────
    dialog_sel = (
        "div:has-text('Discard this post'), "           # confirmation dialog
        "div:has-text('wasn\\'t saved'), "              # variant A
        "div:has-text('unfinished post'), "             # variant B
        "div:has-text('Continue editing'), "            # variant C
        "div:has-text('and any edit'), "                # TUXModal text (fallback)
        "div:has-text('will be discarded'), "           # TUXModal text (fallback)
        "[class*='modal']:has-text('discard'), "        # generic modal (lowercase m)
        "[class*='dialog']:has-text('discard')"
    )
    try:
        page.wait_for_selector(dialog_sel, timeout=3_000)
    except Exception:
        return  # no further draft dialogs — nothing to do

    ui_log("TikTok: draft dialog detected — discarding ...")

    # Pass 1 — handle 'Continue editing?' or 'unfinished post' banner.
    # Click Discard in the BANNER only; this triggers the confirmation modal.
    _click_discard_btn(page, ui_log, "draft-recovery banner")

    # Pass 2 — handle 'Discard this post?' confirmation modal.
    #
    # IMPORTANT: we cannot reuse _click_discard_btn here.  That helper uses
    # locator(...).first, which finds the BANNER'S 'Discard' button (still
    # present in the DOM behind the modal) instead of the modal's own button.
    # Instead, scope the lookup to the dialog container first; if that fails,
    # use .last (modal renders after the banner in DOM order).
    try:
        page.wait_for_selector("div:has-text('Discard this post')", timeout=4_000)
        ui_log("TikTok: clicking Discard on confirmation modal ...")
        clicked = False
        for sel in (
            "[role='dialog'] button:has-text('Discard')",
            "[role='alertdialog'] button:has-text('Discard')",
            "[class*='Modal'] button:has-text('Discard')",
            "[class*='modal'] button:has-text('Discard')",
            "[class*='Dialog'] button:has-text('Discard')",
        ):
            try:
                btn = page.locator(sel).first
                if btn.is_visible(timeout=1_000):
                    btn.click()
                    time.sleep(1.5)
                    clicked = True
                    break
            except Exception:
                continue
        if not clicked:
            # Last resort: .last is the final 'Discard' in DOM order = modal
            try:
                btn = page.locator("button:has-text('Discard')").last
                if btn.is_visible(timeout=1_000):
                    btn.click()
                    time.sleep(1.5)
            except Exception:
                pass
    except Exception:
        pass  # confirmation didn't appear — already gone after pass 1

    # Settle wait: TikTok re-renders the upload form after final discard
    time.sleep(2)
    ui_log("TikTok: draft discarded, upload form should be fresh.")


def _dismiss_any_tux_modal(page, ui_log) -> None:
    """Close any open TUXModal-overlay that would intercept pointer events.

    TikTok's design system uses class='TUXModal-overlay' with
    data-transition-status='open'.  CSS [class*='modal'] is case-sensitive
    and would NOT match 'TUXModal', which is why it was missed before.

    Button priority:
      Discard  — dismiss draft/post confirmation (always correct when uploading)
      Close / Got it / OK / Continue — advisory banners
      Escape   — last-resort keyboard dismiss

    Called at the start of _dismiss_draft_recovery, after _dismiss_advisory_dialogs,
    and directly before any caption/schedule click so a modal can never block us.
    """
    for _attempt in range(4):   # handle up to 4 stacked TUXModals
        try:
            overlay = page.locator(
                "[class*='TUXModal-overlay'][data-transition-status='open'], "
                "[data-floating-ui-portal] [data-transition-status='open']"
            ).first
            if not overlay.is_visible(timeout=1_500):
                break
        except Exception:
            break

        clicked = False
        for btn_text in ("Discard", "Close", "Got it", "OK", "Continue"):
            try:
                btn = overlay.locator(f"button:has-text('{btn_text}')").first
                if btn.is_visible(timeout=500):
                    ui_log(f"TikTok: closing TUXModal ('{btn_text}') ...")
                    btn.click()
                    time.sleep(1.2)
                    clicked = True
                    break
            except Exception:
                continue
        if not clicked:
            page.keyboard.press("Escape")
            time.sleep(0.5)
            break


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


def _dismiss_advisory_dialogs(page, frame, ui_log) -> None:
    """Dismiss TikTok advisory/warning dialogs that appear after video upload.

    Known dialogs (any can appear, any can be absent):
    - Copyright / music warning   → Close / Got it / OK
    - Age-restriction notice      → Continue / OK
    - Auto-caption offer          → Close / No thanks
    - 'Some features unavailable' → Close / OK
    - 'Processing may take time'  → OK / Continue

    Strategy: scan for visible modal-like containers and click any
    dismiss button inside them.  Repeat up to 5 times so stacked dialogs
    all get cleared.  If nothing is visible after a short wait, return.
    """
    dismiss_btns = (
        "button:has-text('Close')",
        "button:has-text('Got it')",
        "button:has-text('OK')",
        "button:has-text('Continue')",
        "button:has-text('No thanks')",
        "button:has-text('Dismiss')",
        "[aria-label='Close']",
    )
    # TikTok may also surface advisory content inside a TUXModal overlay.
    _dismiss_any_tux_modal(page, ui_log)

    for _attempt in range(5):
        found_any = False
        for sel in dismiss_btns:
            try:
                # Check both the main page and the upload iframe
                for ctx in (page, frame):
                    btn = ctx.locator(sel).first
                    if btn.is_visible(timeout=800):
                        ui_log(f"TikTok: dismissing advisory dialog ({sel}) ...")
                        btn.click()
                        time.sleep(0.8)
                        found_any = True
                        break
            except Exception:
                continue
        if not found_any:
            break  # no more visible dialogs


def _tiktok_upload(page, mp4_path: str, caption: str, schedule_dt: datetime, ui_log):
    # Accept native "Leave this page?" / beforeunload browser dialogs automatically
    # so they never block navigation or uploads.
    page.on("dialog", lambda d: d.accept())

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

    # Caption-field selectors, ordered from most-specific to broadest.
    # TikTok Studio uses a Draft.js rich-text editor; we cover multiple versions
    # of their class names plus generic contenteditable fallbacks.
    _CAPTION_SELS = (
        # data-e2e attribute (most stable — TikTok keeps these for automation)
        "[data-e2e='upload-caption']",
        "[data-e2e='caption-input']",
        # data-placeholder attribute text (common in current TikTok Studio)
        "div[contenteditable='true'][data-placeholder*='caption' i]",
        "div[contenteditable='true'][data-placeholder*='describe' i]",
        "div[contenteditable='true'][data-placeholder*='tell' i]",
        # Draft.js specific markers
        "[data-text='true'][contenteditable='true']",
        ".DraftEditor-editorContainer [contenteditable='true']",
        "[data-contents='true'] [contenteditable='true']",
        # Class-based fallbacks
        "[contenteditable='true'][class*='caption']",
        "[contenteditable='true'][class*='editor-kit']",
        "textarea[placeholder*='caption' i]",
        # Broadest: any visible contenteditable in the frame
        "div[contenteditable='true']",
    )
    _caption_sel = ", ".join(_CAPTION_SELS)

    ui_log("TikTok: waiting for upload to complete ...")
    deadline = time.time() + 600   # 10 minutes — large videos need more time
    _last_diag = time.time()
    while time.time() < deadline:
        caption_field = frame.locator(_caption_sel).first
        try:
            if caption_field.is_visible():
                break
            # Every 60 s log a brief diagnostic so we can see what IS visible
            if time.time() - _last_diag >= 60:
                _last_diag = time.time()
                elapsed = int(time.time() - (deadline - 600))
                try:
                    # Count any contenteditable elements (signals caption ready)
                    ce_count = frame.locator("[contenteditable='true']").count()
                    # Check if upload progress indicator is still present
                    prog = frame.locator(
                        "[class*='progress'], [class*='uploading'], "
                        "[aria-label*='upload' i][role='progressbar']"
                    ).count()
                    ui_log(
                        f"TikTok: still waiting … {elapsed}s elapsed, "
                        f"contenteditable={ce_count}, progress-els={prog}"
                    )
                except Exception:
                    ui_log(f"TikTok: still waiting … {elapsed}s elapsed")
        except Exception as _exc:
            if "closed" in str(_exc).lower() or "TargetClosed" in type(_exc).__name__:
                _screenshot_on_fail(page, "frame_closed_during_upload")
                raise RuntimeError(
                    "TikTok upload iframe was closed while waiting for the upload to finish. "
                    "This usually means an undismissed dialog navigated the page away. "
                    "Check temp/fail_frame_closed_*.png for the page state."
                ) from _exc
            raise
        time.sleep(3)
    else:
        _screenshot_on_fail(page, "upload_timeout")
        raise TiktokUploadTimeoutError(
            "TikTok upload did not finish within 10 minutes "
            "(caption field never appeared). "
            "Check temp/fail_upload_timeout_*.png for the page state."
        )

    # ── Post-upload dialogs ───────────────────────────────────────────────────
    # TikTok may show advisory dialogs immediately after a video finishes
    # processing: copyright/music warnings, age-restriction notices, auto-
    # caption confirmation, "some features unavailable" banners, etc.
    # Dismiss them all before touching the caption field.
    _dismiss_advisory_dialogs(page, frame, ui_log)

    # ── Caption ───────────────────────────────────────────────────────────────
    # TikTok's editor is a Slate/Draft.js contenteditable div.
    # Re-query fresh (don't reuse the loop reference — TikTok may have
    # re-rendered after upload finished, leaving the old locator stale).
    # Small pause first to let the post-upload UI finish settling.
    time.sleep(2)
    # Final sweep: dismiss any TUXModal that appeared during video processing.
    # This is the most common cause of "intercepts pointer events" on the caption.
    _dismiss_any_tux_modal(page, ui_log)
    # Re-query using the same broad selector set used in the wait loop.
    caption_field = frame.locator(_caption_sel).first
    ui_log("TikTok: filling caption ...")
    _screenshot_on_fail(page, "before_caption")  # diagnostic: always saved for review
    _safe_click(page, caption_field, ui_log, "caption_field")
    # Triple-click selects the whole line reliably in Draft.js (Ctrl+A sometimes
    # selects across block boundaries but misses embedded nodes).
    caption_field.click(click_count=3)
    caption_field.press("Control+a")
    caption_field.press("Backspace")
    page.keyboard.type(caption[:2200])
    # Draft.js shows autocomplete popups for '#' (hashtags) and '@' (mentions).
    # Escape closes any open popup without losing the typed text.
    page.keyboard.press("Escape")
    time.sleep(0.5)

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

    # TikTok Studio uses custom React date/time pickers.  They may render as
    # native <input type="date|time"> or as styled <input type="text">.
    # Strategy: locate, triple-click to select all existing text, type the
    # value, then dispatch React's synthetic events to commit it.
    # We try two date formats in case TikTok expects MM/DD/YYYY instead of
    # YYYY-MM-DD (varies by TikTok Studio version/locale).
    date_input = frame.locator(
        "input[type='date'], "
        "input[placeholder*='date' i], "
        "input[class*='date' i], "
        "input[class*='Date']"
    ).first
    try:
        date_input.wait_for(state="visible", timeout=5_000)
        date_input.click(click_count=3)
        # Try YYYY-MM-DD first (ISO / Chrome native date input format)
        date_input.fill(schedule_dt.strftime("%Y-%m-%d"))
        date_input.dispatch_event("input")
        date_input.dispatch_event("change")
        time.sleep(0.3)
        # If the field still shows an unrecognised value, try MM/DD/YYYY
        current_val = date_input.input_value()
        if not current_val or current_val in ("", "undefined"):
            date_input.click(click_count=3)
            date_input.type(schedule_dt.strftime("%m/%d/%Y"))
            date_input.dispatch_event("input")
            date_input.dispatch_event("change")
        date_input.press("Tab")
        time.sleep(0.5)
    except Exception:
        _screenshot_on_fail(page, "date_picker_missing")
        raise RuntimeError(
            "TikTok date picker not found after enabling schedule toggle. "
            "Check temp/fail_date_picker_missing_*.png."
        )

    time_input = frame.locator(
        "input[type='time'], "
        "input[placeholder*='time' i], "
        "input[class*='time' i], "
        "input[class*='Time']"
    ).first
    try:
        time_input.wait_for(state="visible", timeout=5_000)
        time_input.click(click_count=3)
        # Try 24-h first; if blank, fall back to 12-h AM/PM (some TikTok locales)
        time_input.fill(schedule_dt.strftime("%H:%M"))
        time_input.dispatch_event("input")
        time_input.dispatch_event("change")
        time.sleep(0.3)
        current_val = time_input.input_value()
        if not current_val or current_val in ("", "undefined"):
            time_input.click(click_count=3)
            time_input.type(schedule_dt.strftime("%I:%M %p"))  # e.g. "03:00 PM"
            time_input.dispatch_event("input")
            time_input.dispatch_event("change")
        time_input.press("Tab")
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

    # ── Confirm intermediate dialog (if any) ─────────────────────────────────
    for _confirm_sel in ("button:has-text('Confirm')", "button:has-text('OK')"):
        try:
            btn = frame.locator(_confirm_sel).first
            if btn.is_visible(timeout=1_500):
                _safe_click(page, btn, ui_log, "confirm_button")
                time.sleep(2)
                break
        except Exception:
            pass

    # ── Verify success / surface errors ──────────────────────────────────────
    # TikTok shows a green banner or navigates after successful scheduling.
    # Catch validation errors (bad time, processing not done, etc.) so the
    # log shows a useful message instead of silently thinking it succeeded.
    _screenshot_on_fail(page, "after_schedule")  # always saved — post-submit state
    time.sleep(1)
    _error_sels = (
        "div:has-text('Please set a valid')",
        "div:has-text('scheduled time must be')",
        "div:has-text('minimum') :has-text('minutes')",
        "div:has-text('Something went wrong')",
        "div:has-text('failed to')",
        "[class*='error']:has-text('schedule')",
        "[class*='error']:has-text('time')",
    )
    for sel in _error_sels:
        try:
            el = frame.locator(sel).first
            if el.is_visible(timeout=500):
                msg = el.inner_text(timeout=500).strip()[:200]
                _screenshot_on_fail(page, "schedule_validation_error")
                raise RuntimeError(f"TikTok scheduling validation error: {msg!r}")
        except RuntimeError:
            raise
        except Exception:
            continue

    ui_log("TikTok: post scheduled.")
